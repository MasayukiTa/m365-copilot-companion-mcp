// ChatDecisionsOriginal.cs -- THE ORACLE. Test-only. In no Build line; nothing ships it.
//
// This is the chat window's send logic EXACTLY as it stood in ui/CopilotChat.cs at commit
// f26c4de016dfe155fbbf8fe210d4bfae8122d153 -- the commit before that logic was extracted into
// ui/ChatSend.cs. The method bodies below are copied from that commit; the long explanatory
// comments were left behind (they moved to ui/ChatSend.cs with the code) and every other line
// is the original unless it carries an ADAPTER marker.
//
// WHAT IT IS FOR. ui/test_the_chat_window_sends_what_was_typed.py runs this and the extracted
// ui/ChatSend.cs over the same cases, against the same recording fakes, and requires the two to
// agree field by field. So the expected outputs of that test come from RUNNING the code that
// shipped, not from what anyone believed it did. That is the only reason this file exists.
//
// DO NOT EDIT IT TO MAKE A TEST PASS. If the extracted code and this disagree, either the
// extraction changed behaviour (fix ChatSend.cs) or the change is deliberate -- in which case
// the test declares the divergence by name and says why, and this file stays as it is.
//
// DECLARED DIFFERENCES (this file intentionally keeps the OLD behaviour; ui/ChatSend.cs has the
// fix; ui/test_the_chat_window_sends_what_was_typed.py's `_EXPECTED_DIVERGENCE` and
// `_DECLARED_BEHAVIOUR_CHANGES` name exactly which cases exercise each one):
//   1. TERMINAL STATUSES (2026-09-24). LiveWorkerFor / ReadActiveFleetWorkerCount below still
//      lack "maxturns" and "content_refused" -- both are in relay/relay_fleet.py's own TERMINAL
//      set. So this oracle still steers a worker that has already used its whole turn budget
//      or had its prompt declined, and still counts it toward the chip's active total.
//   2. CAPACITY REROUTE WRITE RESULT (2026-09-24). DoSend's capacity branch above ignores
//      AppendCommand's return value, exactly as the shipped f26c4de code did -- a write that
//      failed still clears the composer and says "queued".
//   3. CAPACITY REROUTE VS. A FLEET CONVERSATION (2026-09-24). DoSend's capacity branch fires
//      before it asks whether the target conversation is a fleet one, exactly as shipped -- a
//      follow-up typed into a fleet conversation while the fleet is full still becomes an
//      unlinked bare goal instead of reaching SendToFleetConversation.
//
// ADAPTERS, all of them, and why each is the smallest one available:
//   * `public` on DoSend, SendText, LiveWorkerFor, FleetState and ReadActiveFleetWorkerCount,
//     so the harness can call them. Trailing // comments on copied lines were dropped.
//   * The window's members these bodies touch (_input, _send, _conv, _pageConv, _all,
//     _bridgeReachable, _routerShown, _sendInFlight, _lang, AddUser, HttpGet, ...) are shims
//     at the bottom of the class that forward to IChatSendEffects -- the same recording world
//     the extracted code is given -- so both sides are observed by the same instrument.
//   * status.json's path: the original derived it from the exe's directory; here it comes
//     from the case (StatusPath), and reading it is noted in the trace, because the extracted
//     code's read is an effect and is noted there too. The READ ITSELF is the original code.
//   * `_input.Text = text; _input.CaretIndex = ...` (put the text back) -> RestoreInput(text).
//   * ShowRecoveryBanner's third argument (a delegate that starts the stack) is not run.
//   * The WPF tail of SendText (pending container, typing dots, Stop button, busy dot, the
//     Stream thread, ClearChips) -> one BeginStream(text, target) call. It is UI, and the
//     thread would make the trace order nondeterministic.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

class OriginalChat
{
    // ── copied from ui/CopilotChat.cs @ f26c4de: DoSend ─────────────────────────────────────
    public void DoSend()
    {
        var text = _input.Text.Trim();
        if (text.Length == 0 || !_send.IsEnabled) return;
        if (text.Equals("/help", StringComparison.OrdinalIgnoreCase))
        {
            _input.Clear();
            AddUser(text);
            AddAssistant(CommandHelpText());
            return;
        }

        if (_conv == null) NewChat();

        if (!_bridgeReachable)
        {
            bool ok;
            try { HttpGet("/conv", 5000); ok = true; } catch { ok = false; }
            _bridgeReachable = ok;
            if (!ok)
            {
                SetDot("offline");
                AddAssistant(T("send_offline"));
                /* ADAPTER: _input.Text = text; _input.CaretIndex = _input.Text.Length; */
                RestoreInput(text);
                ShowRecoveryBanner(T("send_offline"), T("retry_start_stack"), delegate
                {
                    /* ADAPTER: HideBanner() + start the stack through wscript -- not run here */
                });
                return;
            }
            RefreshIdleDot();
        }

        int[] fs = FleetState();
        if (fs[0] == 1 && fs[2] > 0 && fs[1] >= fs[2] && !text.StartsWith("/"))
        {
            bool force = text.StartsWith("!");
            string body = force ? text.Substring(1).Trim() : text;
            if (body.Length == 0) return;
            _input.Clear(); HideRouter();
            EnqueueToFleet(body, force);
            AddUser(text);
            AddAssistant(force ? T("fleet_forced") : T("fleet_queued"));
            return;
        }

        if (!text.StartsWith("/") && !_routerShown && DetectResearch(text))
        {
            ShowRouter(text);
            return;
        }
        HideRouter();
        _input.Clear();
        SendText(text);
    }

    // ── copied: SendText ────────────────────────────────────────────────────────────────────
    public void SendText(string text)
    {
        Conversation target = _conv;

        if (target.Source == "fleet")
        {
            _input.Clear(); HideRouter();
            SendToFleetConversation(target, text);
            return;
        }

        if (!ReferenceEquals(target, _pageConv))
        {
            string sref = target.ConvUrl ?? "";
            if (sref.StartsWith("sess:"))
            {
                string sguid = sref.Substring("sess:".Length);
                try { HttpGet("/resume?guid=" + Uri.EscapeDataString(sguid), 30000); _pageConv = target; }
                catch { AddAssistant(T("send_wrong_page")); return; }
            }
            else if (!string.IsNullOrEmpty(target.ConvUrl))
            {
                try { HttpGet("/switch?url=" + Uri.EscapeDataString(target.ConvUrl), 15000); _pageConv = target; }
                catch { AddAssistant(T("send_wrong_page")); return; }
            }
            else if (target.Source == "chat" && !string.IsNullOrEmpty(target.Name))
            {
                try { HttpGet("/resume?sid=" + Uri.EscapeDataString(target.Name), 20000);
                      _pageConv = target; }
                catch { AddAssistant(T("send_wrong_page")); return; }
            }
            else if (target.Messages.Count == 0)
            {
                try { HttpGet("/new", 15000); _pageConv = target; }
                catch { AddAssistant(T("send_wrong_page")); return; }
            }
            else
            {
                AddAssistant(T("send_unknown_conv"));
                return;
            }
        }

        _sendInFlight = true;
        target.Messages.Add(new Msg("U", text));
        if (target.Untitled()) { target.Title = TrimTitle(text, 40); }
        if (!_all.Contains(target)) { _all.Insert(0, target); }
        RefreshConvList();
        AddUser(text);
        /* ADAPTER: the WPF tail (AddAssistantContainer, MakeTyping, _generating, _send.Content,
           PaintSend, SetDot("busy"), new Thread(Stream).Start(), ClearChips) */
        BeginStream(text, target);
    }

    // ── copied: LiveWorkerFor ───────────────────────────────────────────────────────────────
    public string LiveWorkerFor(Conversation c)
    {
        try
        {
            if (c == null || string.IsNullOrEmpty(c.Transcript)) return "";
            string sp = StatusPathRead();   // ADAPTER: was Path.GetFullPath(Path.Combine(BaseDirectory, "..", ".fleet", "status.json"))
            if (!File.Exists(sp)) return "";
            string txt;
            using (var fsr = new FileStream(sp, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8)) txt = sr.ReadToEnd();
            var d = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (d == null) return "";
            if (!(d.ContainsKey("running") && Convert.ToBoolean(d["running"]))) return "";
            if (!(d.ContainsKey("workers") && d["workers"] is object[])) return "";
            foreach (object o in (object[])d["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                if (!string.Equals(SS(w, "transcript"), c.Transcript, StringComparison.OrdinalIgnoreCase)) continue;
                string st = SS(w, "status");
                if (st == "done" || st == "resolved" || st == "failed" || st == "error"
                    || st == "cancelled" || st == "stopped" || st == "stuck" || st == "pending") return "";
                return SS(w, "name");
            }
        }
        catch { }
        return "";
    }

    // ── copied: SendToFleetConversation ─────────────────────────────────────────────────────
    void SendToFleetConversation(Conversation c, string text)
    {
        AddUser(text);
        c.Messages.Add(new Msg("U", text));
        string live = LiveWorkerFor(c);

        const string NEW_GOAL_PREFIX = "/goal ";
        bool forceNewGoal = text.StartsWith(NEW_GOAL_PREFIX, StringComparison.OrdinalIgnoreCase);
        if (forceNewGoal)
        {
            text = text.Substring(NEW_GOAL_PREFIX.Length).Trim();
            if (text.Length == 0) { AddAssistant(T("fleet_goal_empty")); StickToEnd(); return; }
            live = "";
        }

        if (live.Length > 0)
        {
            var it = new Dictionary<string, object>(); it["worker"] = live; it["text"] = text;
            AddAssistant(AppendCommand("steer", it) ? T("fleet_steer_sent") : T("fleet_send_failed"));
            RefreshConvList();
            StickToEnd();
            return;
        }
        string goal = (c.Goal ?? "").Trim();
        if (goal.Length == 0)
        {
            AddAssistant(T("fleet_no_goal"));
            StickToEnd();
            return;
        }
        var g = new Dictionary<string, object>();
        g["text"] = forceNewGoal ? text : (_lang == 0
            ? "【ユーザーからの追加指示】" + text + "\n直前までの作業内容を踏まえ、この追加指示に対してだけ答えてください。最初からやり直す必要はありません。完了なら DONE、無理なら FAIL と理由を書いてください。"
            : "[follow-up from the user] " + text + "\nAnswer only this follow-up, building on the work so far. Do not start over. Write DONE when finished, or FAIL and why.");
        if (c.ConvUrl != null && c.ConvUrl.StartsWith("sess:", StringComparison.OrdinalIgnoreCase))
            g["resume_conv"] = c.ConvUrl;
        g["follow_up_to"] = goal;
        g["priority"] = true;
        if (forceNewGoal) g["new_task"] = true;
        bool ok = AppendCommand("add_goal", g);
        if (!ok) { AddAssistant(T("fleet_send_failed")); StickToEnd(); return; }
        AddAssistant(FleetState()[0] == 1 ? T("fleet_follow_sent") : T("fleet_follow_idle"));
        RefreshConvList();
        StickToEnd();
    }

    // ── copied: FleetState ──────────────────────────────────────────────────────────────────
    public int[] FleetState()
    {
        try
        {
            string sp = StatusPathRead();   // ADAPTER: was Path.GetFullPath(Path.Combine(BaseDirectory, "..", ".fleet", "status.json"))
            if (!File.Exists(sp)) return new int[] { 0, 0, 0 };
            string txt;
            using (var fsr = new FileStream(sp, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8)) txt = sr.ReadToEnd();
            var d = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (d == null) return new int[] { 0, 0, 0 };
            bool running = d.ContainsKey("running") && Convert.ToBoolean(d["running"]);
            bool idle = d.ContainsKey("idle") && Convert.ToBoolean(d["idle"]);
            int open = d.ContainsKey("open_tabs") && d["open_tabs"] != null ? Convert.ToInt32(d["open_tabs"]) : 0;
            int maxc = d.ContainsKey("max_concurrent") && d["max_concurrent"] != null ? Convert.ToInt32(d["max_concurrent"]) : 0;
            return new int[] { (running && !idle) ? 1 : 0, open, maxc };
        }
        catch { return new int[] { 0, 0, 0 }; }
    }

    // ── copied: ReadActiveFleetWorkerCount (the chip's count; the second terminal list) ─────
    public int ReadActiveFleetWorkerCount()
    {
        try
        {
            string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(sp)) return 0;
            string txt;
            using (var fsr = new FileStream(sp, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8)) txt = sr.ReadToEnd();
            var d = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (d == null || !d.ContainsKey("workers") || !(d["workers"] is object[])) return 0;
            int count = 0;
            foreach (object o in (object[])d["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                string st = SS(w, "status").ToLowerInvariant();
                if (st == "pending" || st == "done" || st == "resolved" || st == "failed"
                    || st == "error" || st == "cancelled" || st == "stopped") continue;
                count++;
            }
            return count;
        }
        catch { return 0; }
    }

    // ── copied: EnqueueToFleet, DetectResearch + its hints, TrimTitle, SS ───────────────────
    void EnqueueToFleet(string text, bool priority)
    {
        var item = new Dictionary<string, object>();
        item["text"] = text; item["priority"] = priority;
        AppendCommand("add_goal", item);
    }

    static readonly string[] _researchHints = {
        "調査", "調べて", "深掘り", "リサーチ", "最新情報", "出典", "比較して", "下調べ",
        "research", "investigate", "look up", "deep dive", "find out", "compare "
    };
    bool DetectResearch(string msg)
    {
        string m = msg.ToLower();
        foreach (var h in _researchHints) if (m.Contains(h.ToLower())) return true;
        return false;
    }

    static string TrimTitle(string s, int max)
    {
        if (string.IsNullOrEmpty(s)) return s;
        int nl = s.IndexOfAny(new[] { '\r', '\n' });
        if (nl >= 0) s = s.Substring(0, nl);
        s = s.Trim();
        if (s.Length > max) s = s.Substring(0, max) + "…";
        return s;
    }

    static string SS(Dictionary<string, object> d, string k)
    { return (d.ContainsKey(k) && d[k] != null) ? d[k].ToString() : ""; }

    readonly JavaScriptSerializer _cjs = new JavaScriptSerializer();

    // ═══ ADAPTER SHIMS -- everything below stands in for a member of ChatWindow ══════════════
    readonly IChatSendEffects W;
    readonly Func<string> _statusRead;   // notes the read in the trace, returns the case's path
    readonly string _convsPath;          // beside status.json, as in the window

    public OriginalChat(IChatSendEffects world, Func<string> statusRead, string convsPath)
    {
        W = world; _statusRead = statusRead; _convsPath = convsPath;
        _input = new InputShim(world); _send = new SendShim(world);
    }

    string StatusPathRead() { return _statusRead(); }

    class InputShim
    {
        readonly IChatSendEffects w;
        public InputShim(IChatSendEffects w) { this.w = w; }
        public string Text { get { return w.InputText(); } }
        public void Clear() { w.ClearInput(); }
    }
    class SendShim
    {
        readonly IChatSendEffects w;
        public SendShim(IChatSendEffects w) { this.w = w; }
        public bool IsEnabled { get { return w.SendEnabled(); } }
    }
    readonly InputShim _input;
    readonly SendShim _send;

    Conversation _conv { get { return W.CurrentConversation(); } }
    Conversation _pageConv { get { return W.PageConversation(); } set { W.SetPageConversation(value); } }
    List<Conversation> _all { get { return W.AllConversations(); } }
    bool _bridgeReachable { get { return W.BridgeReachable(); } set { W.SetBridgeReachable(value); } }
    bool _routerShown { get { return W.RouterShown(); } }
    bool _sendInFlight { set { if (value) W.MarkSendInFlight(); } }
    int _lang { get { return W.Lang(); } }

    string T(string k) { return W.T(k); }
    string CommandHelpText() { return W.CommandHelpText(); }
    void AddUser(string text) { W.AddUser(text); }
    void AddAssistant(string text) { W.AddAssistant(text); }
    void NewChat() { W.NewChat(); }
    string HttpGet(string path, int timeoutMs) { return W.HttpGet(path, timeoutMs); }
    void SetDot(string state) { W.SetDot(state); }
    void RestoreInput(string text) { W.RestoreInput(text); }
    void ShowRecoveryBanner(string message, string buttonLabel, Action onClick) { W.ShowStartStackBanner(message, buttonLabel); }
    void RefreshIdleDot() { W.RefreshIdleDot(); }
    void HideRouter() { W.HideRouter(); }
    void ShowRouter(string text) { W.ShowRouter(text); }
    bool AppendCommand(string key, object item) { return W.AppendCommand(key, item); }
    void RefreshConvList() { W.RefreshConvList(); }
    void StickToEnd() { W.StickToEnd(); }
    void BeginStream(string text, Conversation target) { W.BeginStream(text, target); }
}
