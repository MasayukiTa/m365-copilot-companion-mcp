// ChatSend.cs -- what the chat window DOES with a line that was typed, as code a test can run.
//
// WHY THIS IS ITS OWN FILE. Until 2026-09-24 the whole send path -- the capacity reroute, the
// "!" priority prefix, the research router, the page-pinning doors, the fleet steer / follow-up
// / `/goal` split and the payload that goes on the fleet command channel -- lived inside
// ChatWindow, a WPF class that cannot be constructed without a dispatcher, a bridge and a
// desktop. Every test of it asserted on the text of ui/CopilotChat.cs. A text assertion says
// a spelling is present; it cannot say which branch a given input takes, and it cannot see an
// effect happen in the wrong order or not at all. ui/test_a_fleet_conversation_can_be_answered.py
// was green, for example, while relay/fleet_runner.goals_from_command dropped resume_conv.
//
// So the decisions are pure functions here and the orchestration takes its effects through
// IChatSendEffects. ChatWindow supplies the real effects; ui/test_the_chat_window_sends_what_was_typed.py
// compiles this file with csc, supplies fakes that record every call in order, and compares
// the result with the pre-extraction code (ui/testdata/ChatDecisionsOriginal.cs) case by case.
//
// WHAT STAYS UNTESTED. The WPF key event reaching ChatWindow.DoSend, and the real effects
// behind the interface: the HTTP call to the bridge, the stream, what AddAssistant draws.
//
// Compiled into CopilotChat.exe only (ui/rebuild_ui.ps1's CopilotChat Build line), and
// standalone by that test. Legacy csc (Framework64 v4.0.30319, C# 5): NO string interpolation,
// NO null-conditional, NO expression-bodied members, NO nameof. No WPF types in this file --
// that is what lets the test compile it without a desktop.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

// Moved here unchanged from ui/CopilotChat.cs so the send path can be compiled without WPF.
class Msg { public string Role; public string Text; public Msg(string r, string t) { Role = r; Text = t; } }

class Conversation
{
    public string Id = Guid.NewGuid().ToString("N").Substring(0, 12);
    public string Title = "";        // empty = untitled (shows the localized default)
    public string ConvUrl = "";
    public string Source = "";
    public double Ts = 0;
    public string Transcript = "";   // disk jsonl path (fleet convs) -> open from disk, no scrape
    public string Name = "";         // worker name (fallback to resolve the transcript by name)
    public string Goal = "";         // fleet: the FULL goal text -- what identifies this
                                     // conversation to socket_route.conversation_for_goal,
                                     // so a follow-up can continue it. Title is truncated
                                     // for display and must never be used for this.
    public List<Msg> Messages = new List<Msg>();
    public bool Untitled() { return string.IsNullOrEmpty(Title); }
}

/// Everything the send path reads from, or does to, the window and the world. ChatWindow
/// implements it with the real thing; the test implements it with a recorder.
interface IChatSendEffects
{
    // ── reads (no side effect) ──
    string InputText();                    // the composer's raw text
    bool SendEnabled();                    // the Send button is enabled
    Conversation CurrentConversation();    // _conv (may be null)
    Conversation PageConversation();       // _pageConv: what the bridge page is believed to show
    List<Conversation> AllConversations(); // _all, the sidebar's list -- mutated in place
    bool BridgeReachable();                // last known reachability of the bridge
    bool RouterShown();                    // the research-router bar is up
    int Lang();                            // 0 = Japanese, 1 = English
    string T(string key);                  // localized string
    string CommandHelpText();
    /// .fleet/status.json as text; null when the file does not exist. MAY THROW (a locked or
    /// unreadable file): every caller here turns that into the "no fleet" answer.
    string ReadFleetStatus();

    // ── effects ──
    void ClearInput();
    void RestoreInput(string text);        // put the text back in the composer, caret at its end
    void AddUser(string text);
    void AddAssistant(string text);
    void NewChat();
    string HttpGet(string path, int timeoutMs);   // to the bridge; throws on any failure
    void SetBridgeReachable(bool ok);
    void SetDot(string state);
    void ShowStartStackBanner(string message, string buttonLabel);  // its button starts the stack
    void RefreshIdleDot();
    void HideRouter();
    void ShowRouter(string text);
    bool AppendCommand(string key, object item);  // one fleet command; false = not written
    void SetPageConversation(Conversation c);
    void MarkSendInFlight();
    void RefreshConvList();
    void BeginStream(string text, Conversation target);  // the typing dots + the Stream thread
    void StickToEnd();
}

static class ChatSend
{
    // ═══ PURE DECISIONS ═══════════════════════════════════════════════════════════════════

    /// A worker in one of these statuses is not running a turn, so a steer addressed to it
    /// would never be taken and it is not "active". The terminal statuses, plus "pending"
    /// (queued, not started). ONE LIST FOR BOTH READERS: this used to be written out twice in
    /// ui/CopilotChat.cs -- LiveWorkerFor's copy said it "mirrors" ReadActiveFleetWorkerCount's,
    /// and it did not: the chip's copy had no "stuck", so a stuck worker was counted as active
    /// while relay/relay_fleet.py lists "stuck" among its terminal statuses. The chip now agrees
    /// with LiveWorkerFor. EXACT, CASE-SENSITIVE MATCH: the chip lowercases before asking, the
    /// steer lookup never did, and each caller keeps what it did.
    ///
    /// NOT HERE, AND KNOWN: relay's terminal set also has "maxturns" (and idle_edge.py
    /// "content_refused"). Neither list here ever had them; adding them would change what both
    /// readers do, which this extraction does not.
    public static bool IsTerminalWorkerStatus(string status)
    {
        return status == "done" || status == "resolved" || status == "failed" || status == "error"
            || status == "cancelled" || status == "stopped" || status == "stuck" || status == "pending";
    }

    public static string SS(Dictionary<string, object> d, string k)
    { return (d.ContainsKey(k) && d[k] != null) ? d[k].ToString() : ""; }

    // First line only, ellipsis-trimmed to `max` chars. Shared by SendText (stored title) and the
    // sidebar/header DISPLAY of long saved titles so a whole first message never fills a row.
    public static string TrimTitle(string s, int max)
    {
        if (string.IsNullOrEmpty(s)) return s;
        int nl = s.IndexOfAny(new[] { '\r', '\n' });
        if (nl >= 0) s = s.Substring(0, nl);
        s = s.Trim();
        if (s.Length > max) s = s.Substring(0, max) + "…";
        return s;
    }

    // ── #2 research-intent detection ──
    static readonly string[] _researchHints = {
        "調査", "調べて", "深掘り", "リサーチ", "最新情報", "出典", "比較して", "下調べ",
        "research", "investigate", "look up", "deep dive", "find out", "compare "
    };
    public static bool DetectResearch(string msg)
    {
        string m = msg.ToLower();
        foreach (var h in _researchHints) if (m.Contains(h.ToLower())) return true;
        return false;
    }

    static readonly JavaScriptSerializer _js = new JavaScriptSerializer();

    /// status.json as text, or null when there is no such file. Opened FileShare.ReadWrite
    /// because the fleet replaces it about once a second. Throws on an unreadable file; the
    /// callers below decide what that means.
    public static string ReadStatusFile(string path)
    {
        if (!File.Exists(path)) return null;
        using (var fsr = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        using (var sr = new StreamReader(fsr, Encoding.UTF8)) return sr.ReadToEnd();
    }

    /// [running(0/1), openTabs, maxConcurrent] from status.json's text (null = no file).
    /// FIELD CONTRACT, each exactly as the window always read it:
    ///   running  -- 1 only when "running" is present and truthy AND "idle" is absent or falsy
    ///   open_tabs, max_concurrent -- absent or null -> 0; otherwise Convert.ToInt32
    ///   anything that fails to parse or convert -> {0,0,0} as a whole, never a partial answer
    public static int[] FleetStateOf(string statusText)
    {
        try
        {
            if (statusText == null) return new int[] { 0, 0, 0 };
            var d = _js.DeserializeObject(statusText) as Dictionary<string, object>;
            if (d == null) return new int[] { 0, 0, 0 };
            bool running = d.ContainsKey("running") && Convert.ToBoolean(d["running"]);
            bool idle = d.ContainsKey("idle") && Convert.ToBoolean(d["idle"]);
            int open = d.ContainsKey("open_tabs") && d["open_tabs"] != null ? Convert.ToInt32(d["open_tabs"]) : 0;
            int maxc = d.ContainsKey("max_concurrent") && d["max_concurrent"] != null ? Convert.ToInt32(d["max_concurrent"]) : 0;
            return new int[] { (running && !idle) ? 1 : 0, open, maxc };
        }
        catch { return new int[] { 0, 0, 0 }; }
    }

    // The live worker for a conversation, or "" -- matched on the TRANSCRIPT PATH, which is
    // unique per worker per run. Matching on the worker name instead would be wrong in the
    // ordinary case: "w0" exists in every run there has ever been, and a steer addressed to a
    // name is delivered by name, so an old conversation would steer a stranger.
    //
    // PRECEDENCE, and it is load-bearing:
    //   no transcript on the conversation            -> "" (status.json is not even consulted)
    //   no status.json / unparseable / not an object -> ""
    //   "running" absent or falsy                     -> ""  (a stale file names long-gone workers)
    //   "workers" absent or not an array              -> ""
    //   THE FIRST worker whose transcript equals ours, ignoring case, DECIDES: a terminal or
    //   pending status there returns "" even if a later duplicate row is live. "idle" is not read.
    public static string LiveWorkerFor(string statusText, string transcript)
    {
        try
        {
            if (string.IsNullOrEmpty(transcript)) return "";
            if (statusText == null) return "";
            var d = _js.DeserializeObject(statusText) as Dictionary<string, object>;
            if (d == null) return "";
            if (!(d.ContainsKey("running") && Convert.ToBoolean(d["running"]))) return "";
            if (!(d.ContainsKey("workers") && d["workers"] is object[])) return "";
            foreach (object o in (object[])d["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                if (!string.Equals(SS(w, "transcript"), transcript, StringComparison.OrdinalIgnoreCase)) continue;
                if (IsTerminalWorkerStatus(SS(w, "status"))) return "";
                return SS(w, "name");
            }
        }
        catch { }
        return "";
    }

    /// Workers that are doing something, for the header's "Fleet: N" chip. The status is
    /// LOWERCASED before the terminal check (the steer lookup above does not lowercase).
    /// "running" is not consulted here, and never was.
    public static int CountActiveWorkers(string statusText)
    {
        try
        {
            if (statusText == null) return 0;
            var d = _js.DeserializeObject(statusText) as Dictionary<string, object>;
            if (d == null || !d.ContainsKey("workers") || !(d["workers"] is object[])) return 0;
            int count = 0;
            foreach (object o in (object[])d["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                if (IsTerminalWorkerStatus(SS(w, "status").ToLowerInvariant())) continue;
                count++;
            }
            return count;
        }
        catch { return 0; }
    }

    public static int ActiveWorkerCount(string statusPath)
    {
        try { return CountActiveWorkers(ReadStatusFile(statusPath)); }
        catch { return 0; }
    }

    /// #3: while a fleet is at capacity, a native send would open another heavy tab and blow
    /// the memory budget, so it goes into the fleet queue instead.
    ///   Reroute only when running==1 AND max_concurrent > 0 AND open_tabs >= max_concurrent
    ///   AND the text does not start with "/" (slash commands are never rerouted).
    ///   A leading "!" forces priority; the rest, trimmed, is the goal. "!" and nothing else
    ///   is DROPPED: nothing is sent, nothing is said, and the composer keeps its text.
    public sealed class Capacity
    {
        public bool Reroute;   // queue it for the fleet instead of sending it here
        public bool Drop;      // at capacity, "!" with no text: do nothing at all
        public bool Force;     // priority (the "!" prefix)
        public string Body;    // what is queued
    }

    public static Capacity DecideCapacity(string text, int[] fs)
    {
        var r = new Capacity();
        if (fs[0] == 1 && fs[2] > 0 && fs[1] >= fs[2] && !text.StartsWith("/"))
        {
            bool force = text.StartsWith("!");
            string body = force ? text.Substring(1).Trim() : text;
            if (body.Length == 0) { r.Drop = true; return r; }
            r.Reroute = true; r.Force = force; r.Body = body;
        }
        return r;
    }

    // ── which door puts the bridge page on `target` before a send ──
    public const string DOOR_FLEET = "fleet";              // not on the page at all: SendToFleetConversation
    public const string DOOR_ON_PAGE = "on_page";          // the page already shows it: send
    public const string DOOR_RESUME_GUID = "resume_guid";  // GET /resume?guid=, 30 s
    public const string DOOR_SWITCH = "switch";            // GET /switch?url=, 15 s
    public const string DOOR_RESUME_SID = "resume_sid";    // GET /resume?sid=, 20 s
    public const string DOOR_NEW = "new";                  // GET /new, 15 s
    public const string DOOR_UNKNOWN = "unknown";          // refuse: send_unknown_conv

    /// PRECEDENCE, first match wins:
    ///   1. Source == "fleet"                      -> fleet  (decided BEFORE the page doors)
    ///   2. target is the page's conversation      -> on_page
    ///   3. ConvUrl starts with "sess:"            -> resume_guid, arg = what follows "sess:"
    ///      (CASE-SENSITIVE here, unlike the fleet payload's OrdinalIgnoreCase test below --
    ///       "SESS:x" on a chat row goes to /switch, exactly as it always did)
    ///   4. ConvUrl non-empty                      -> switch, arg = ConvUrl
    ///   5. Source == "chat" and Name non-empty    -> resume_sid, arg = Name (Name is the sid on
    ///      a chat row and the worker name on a fleet row, hence the Source gate)
    ///   6. no messages yet                        -> new
    ///   7. otherwise                              -> unknown
    /// A null ConvUrl is treated as "" for 3 and fails 4. `arg` is NOT escaped here: the
    /// escaping happens inside the send's try block, where it always did.
    public static string DecideDoor(Conversation target, Conversation pageConv, out string arg)
    {
        arg = null;
        if (target.Source == "fleet") return DOOR_FLEET;
        if (ReferenceEquals(target, pageConv)) return DOOR_ON_PAGE;
        // A "sess:<guid>" IS NOT A URL AND MUST NOT GO TO /switch. /switch is
        // `release_socket_driver()` followed by `_goto_settled(url)`: it drops the websocket
        // and opens a tab. Every fleet conversation carries that shape, so the ordinary case
        // was also the one that cost a browser -- and the archive rebuild would have made
        // 3,469 of them. /resume takes a guid and binds the session WITHOUT touching the page
        // (socket_route.driver_for(conversation_id=...), measured 2026-08-24).
        string sref = target.ConvUrl ?? "";
        if (sref.StartsWith("sess:")) { arg = sref.Substring("sess:".Length); return DOOR_RESUME_GUID; }
        if (!string.IsNullOrEmpty(target.ConvUrl)) { arg = target.ConvUrl; return DOOR_SWITCH; }
        // A CONVERSATION WITH NO URL IS NOT AUTOMATICALLY UNREACHABLE. One captured over the
        // socket is stored as "sess:<guid>", which is not something you can navigate to, so it
        // registers with url="" and /switch has nothing to take. It does carry its sid, in
        // Name, and /resume takes a sid and knows both stored shapes. Gated on Source, because
        // Name means two different things: the sid for a chat row, and the worker name (w0,
        // w1) for a fleet row. Resuming a fleet row by "w0" would ask the store for a session
        // that does not exist.
        if (target.Source == "chat" && !string.IsNullOrEmpty(target.Name)) { arg = target.Name; return DOOR_RESUME_SID; }
        if (target.Messages.Count == 0) return DOOR_NEW;
        return DOOR_UNKNOWN;
    }

    /// What to put on the fleet channel for text typed into a fleet conversation.
    /// Kind is "refuse" (RefusalKey says why) or "command" (Key + Item).
    public sealed class FleetSend
    {
        public string Kind;
        public string RefusalKey;
        public string Key;
        public Dictionary<string, object> Item;
    }

    /// PRECEDENCE:
    ///   1. "/goal " (any case) + nothing        -> refuse fleet_goal_empty
    ///   2. "/goal " + text                      -> the follow-up path below, EVEN WHILE A
    ///                                              WORKER IS LIVE, with the text as the goal
    ///   3. a live worker (`live` non-empty)     -> steer {worker, text}
    ///   4. no goal text on the conversation     -> refuse fleet_no_goal
    ///   5. otherwise                            -> add_goal
    /// add_goal FIELD CONTRACT, in this key order:
    ///   text         -- /goal: the text itself; else the follow-up framing in `lang`
    ///                   (0 = Japanese, anything else = English)
    ///   resume_conv  -- ONLY when c.ConvUrl starts with "sess:" ignoring case; ABSENT otherwise
    ///   follow_up_to -- c.Goal, trimmed (never c.Title)
    ///   priority     -- true, always
    ///   new_task     -- true ONLY for /goal; ABSENT otherwise
    /// Reads c.Goal and c.ConvUrl and nothing else of c.
    public static FleetSend DecideFleetSend(string text, string live, Conversation c, int lang)
    {
        var r = new FleetSend();
        // AN ESCAPE FROM THE STEER, BECAUSE ONLY THE PERSON TYPING KNOWS WHICH IT IS. While a
        // worker is live everything typed here becomes a steer -- right for "keep going, but
        // do it this way", wrong for "here is a different job". Observed live before
        // 2026-09-16: an unrelated question typed into a running conversation left one worker
        // holding two tasks, judged against only the first, oscillating for the rest of the
        // run. `/goal ` in front routes to the follow-up path instead: its own worker, card and
        // judge, and resume_conv keeps it in this same conversation.
        const string NEW_GOAL_PREFIX = "/goal ";
        bool forceNewGoal = text.StartsWith(NEW_GOAL_PREFIX, StringComparison.OrdinalIgnoreCase);
        if (forceNewGoal)
        {
            text = text.Substring(NEW_GOAL_PREFIX.Length).Trim();
            if (text.Length == 0) { r.Kind = "refuse"; r.RefusalKey = "fleet_goal_empty"; return r; }
            live = "";      // fall through to the follow-up goal path
        }

        if (live.Length > 0)
        {
            var it = new Dictionary<string, object>(); it["worker"] = live; it["text"] = text;
            r.Kind = "command"; r.Key = "steer"; r.Item = it;
            return r;
        }
        // NO GOAL TEXT MEANS NO WAY TO NAME THE CONVERSATION, and guessing is the failure
        // being fixed: a follow-up that silently becomes a fresh chat answers plausibly and
        // is indistinguishable from a real continuation.
        string goal = (c.Goal ?? "").Trim();
        if (goal.Length == 0) { r.Kind = "refuse"; r.RefusalKey = "fleet_no_goal"; return r; }
        var g = new Dictionary<string, object>();
        // A FOLLOW-UP AND A NEW JOB NEED DIFFERENT FRAMING. The wrapper says "build on the work
        // so far and answer only this", which is right for a follow-up and wrong for a
        // different task, so `/goal` travels as the goal itself.
        g["text"] = forceNewGoal ? text : (lang == 0
            ? "【ユーザーからの追加指示】" + text + "\n直前までの作業内容を踏まえ、この追加指示に対してだけ答えてください。最初からやり直す必要はありません。完了なら DONE、無理なら FAIL と理由を書いてください。"
            : "[follow-up from the user] " + text + "\nAnswer only this follow-up, building on the work so far. Do not start over. Write DONE when finished, or FAIL and why.");
        // THE CONVERSATION BY ITS ID. `ConvUrl` is "sess:<guid>", read out of the transcript's
        // own guid line. Until this was added the follow-up carried only `follow_up_to` -- the
        // GOAL TEXT -- and the fleet matched the conversation on that text: identity by
        // wording (docs/incidents/20260912_a_fleet_conversation_could_be_read_and_never_answered.md).
        // relay_fleet takes `resume_conv` through _conversation_id_or_empty, which accepts
        // exactly this shape; `follow_up_to` stays as the fallback for a row with no guid.
        if (c.ConvUrl != null && c.ConvUrl.StartsWith("sess:", StringComparison.OrdinalIgnoreCase))
            g["resume_conv"] = c.ConvUrl;
        g["follow_up_to"] = goal;
        g["priority"] = true;
        // SAY WHICH VERB SENT THIS. A `/goal ` submission and an ordinary follow-up leave the
        // same shape once the command file is consumed; the fleet turns this into one
        // mechanisms.jsonl row, so "has anyone ever used /goal" has an answer.
        if (forceNewGoal) g["new_task"] = true;
        r.Kind = "command"; r.Key = "add_goal"; r.Item = g;
        return r;
    }

    // ═══ ORCHESTRATION -- the decisions above, applied, with every effect through `fx` ═════

    /// status.json read and decided; an unreadable file is "no fleet".
    public static int[] FleetState(IChatSendEffects fx)
    {
        string txt;
        try { txt = fx.ReadFleetStatus(); }
        catch { return new int[] { 0, 0, 0 }; }
        return FleetStateOf(txt);
    }

    /// Only consults status.json when the conversation has a transcript to match on.
    public static string LiveWorkerFor(IChatSendEffects fx, Conversation c)
    {
        try
        {
            if (c == null || string.IsNullOrEmpty(c.Transcript)) return "";
            return LiveWorkerFor(fx.ReadFleetStatus(), c.Transcript);
        }
        catch { return ""; }
    }

    /// The Send button / Enter. In order: empty or disabled -> nothing; /help; make sure there
    /// is a conversation; re-probe an unreachable bridge once (refuse and offer to start the
    /// stack if still down); capacity reroute; research router; SendText.
    public static void DoSend(IChatSendEffects fx)
    {
        var text = fx.InputText().Trim();
        if (text.Length == 0 || !fx.SendEnabled()) return;
        if (text.Equals("/help", StringComparison.OrdinalIgnoreCase))
        {
            fx.ClearInput();
            fx.AddUser(text);
            fx.AddAssistant(fx.CommandHelpText());
            return;
        }

        // #4: new-chat fallback -- nothing to send into yet, so start a fresh conversation first.
        if (fx.CurrentConversation() == null) fx.NewChat();

        // #4: bridge-reachability fallback -- re-probe once synchronously before refusing the
        // send, since reachability is only updated by a low-cadence background probe.
        if (!fx.BridgeReachable())
        {
            bool ok;
            try { fx.HttpGet("/conv", 5000); ok = true; } catch { ok = false; }
            fx.SetBridgeReachable(ok);
            if (!ok)
            {
                fx.SetDot("offline");
                fx.AddAssistant(fx.T("send_offline"));
                fx.RestoreInput(text);   // put the trimmed text back so it isn't lost
                fx.ShowStartStackBanner(fx.T("send_offline"), fx.T("retry_start_stack"));
                return;
            }
            fx.RefreshIdleDot();
        }

        // #3: at capacity -> the fleet queue. Read even for a slash command; only the decision
        // exempts it.
        Capacity cap = DecideCapacity(text, FleetState(fx));
        if (cap.Drop) return;
        if (cap.Reroute)
        {
            fx.ClearInput(); fx.HideRouter();
            var item = new Dictionary<string, object>();
            item["text"] = cap.Body; item["priority"] = cap.Force;
            fx.AppendCommand("add_goal", item);   // result unchecked, as it always was
            fx.AddUser(text);
            fx.AddAssistant(cap.Force ? fx.T("fleet_forced") : fx.T("fleet_queued"));
            return;
        }

        // #2: research-intent auto-router -- propose the researcher (confirm, not auto, to
        // avoid false positives), the way Claude Code surfaces a tool.
        if (!text.StartsWith("/") && !fx.RouterShown() && DetectResearch(text))
        {
            fx.ShowRouter(text);
            return;
        }
        fx.HideRouter();
        fx.ClearInput();
        SendText(fx, text);
    }

    /// Send `text` into the current conversation (also the router bar's two buttons).
    public static void SendText(IChatSendEffects fx, string text)
    {
        // Snapshot the conversation this send targets ONCE, up front. Everything below (and
        // everything in Stream) must operate on `target`, never re-read the current one -- a
        // fleet-card open landing mid-send must not be able to redirect this reply elsewhere.
        Conversation target = fx.CurrentConversation();

        string arg;
        string door = DecideDoor(target, fx.PageConversation(), out arg);
        if (door == DOOR_FLEET)
        {
            // A FLEET CONVERSATION IS NOT ON THE PAGE, so none of the page doors can open it; it
            // belongs to a worker on a socket, and the fleet already accepts messages for one.
            fx.ClearInput(); fx.HideRouter();
            SendToFleetConversation(fx, target, text);
            return;
        }
        if (door == DOOR_UNKNOWN)
        {
            fx.AddAssistant(fx.T("send_unknown_conv"));
            return;
        }
        if (door != DOOR_ON_PAGE)
        {
            // The escape is inside the try, as it always was: a failure to build the path is a
            // failure to reach the page, not a crash.
            try
            {
                if (door == DOOR_RESUME_GUID) fx.HttpGet("/resume?guid=" + Uri.EscapeDataString(arg), 30000);
                else if (door == DOOR_SWITCH) fx.HttpGet("/switch?url=" + Uri.EscapeDataString(arg), 15000);
                else if (door == DOOR_RESUME_SID) fx.HttpGet("/resume?sid=" + Uri.EscapeDataString(arg), 20000);
                else fx.HttpGet("/new", 15000);
                fx.SetPageConversation(target);
            }
            catch { fx.AddAssistant(fx.T("send_wrong_page")); return; }
        }

        fx.MarkSendInFlight();
        target.Messages.Add(new Msg("U", text));
        if (target.Untitled()) { target.Title = TrimTitle(text, 40); }   // first line, max 40 + ellipsis
        var all = fx.AllConversations();
        if (!all.Contains(target)) { all.Insert(0, target); }
        fx.RefreshConvList();
        fx.AddUser(text);
        fx.BeginStream(text, target);
    }

    // Put `text` to the fleet conversation `c`. Live worker -> a steer on its next turn.
    // Otherwise -> a goal carrying resume_conv, the conversation's own durable id, which
    // RelayWorker.__init__ uses directly; follow_up_to is the FALLBACK, for a row captured
    // before guids were recorded.
    static void SendToFleetConversation(IChatSendEffects fx, Conversation c, string text)
    {
        fx.AddUser(text);
        c.Messages.Add(new Msg("U", text));
        string live = LiveWorkerFor(fx, c);
        FleetSend d = DecideFleetSend(text, live, c, fx.Lang());
        if (d.Kind == "refuse")
        {
            fx.AddAssistant(fx.T(d.RefusalKey));
            fx.StickToEnd();
            return;
        }
        if (d.Key == "steer")
        {
            fx.AddAssistant(fx.AppendCommand("steer", d.Item) ? fx.T("fleet_steer_sent") : fx.T("fleet_send_failed"));
            fx.RefreshConvList();
            fx.StickToEnd();
            return;
        }
        bool ok = fx.AppendCommand("add_goal", d.Item);
        if (!ok) { fx.AddAssistant(fx.T("fleet_send_failed")); fx.StickToEnd(); return; }
        fx.AddAssistant(FleetState(fx)[0] == 1 ? fx.T("fleet_follow_sent") : fx.T("fleet_follow_idle"));
        fx.RefreshConvList();
        fx.StickToEnd();
    }
}
