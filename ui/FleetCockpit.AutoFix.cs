// FleetCockpit.AutoFix.cs -- automatic repair before anyone is asked to click: budget, mutex, MaybeAutoFix.
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

    //: How old a capture may be and still describe the present. Beyond this a sign-in
    //: failure is history, not a fault to act on.
    //: 1.4x the token lifetime measured on this machine (3,848s), not a round number
    //: chosen for looking like one. This is the maximum time a broken sign-in may go
    //: unreported, and it is deliberately independent of the issuer's token policy.
    const double SIGNIN_EVIDENCE_MAX_AGE_S = 5400.0;

    // ── automatic repair, before anyone is asked to click ───────────────────────────────────
    //
    // RunFix knows how to repair every state these dots report, and it was reachable ONLY from
    // the button. So a broken agent path stayed broken for as long as nobody happened to be
    // looking at this window. On 2026-08-31 that was hours: the fleet's Edge sat on
    // about:blank, every worker fell back to the assistant with no tools, and two benchmark
    // runs produced patches written from memory.
    //
    // The manual button remains, and it is the point: the automatic path tries first, a
    // bounded number of times, and hands over to the human only when it has genuinely failed.
    const int AUTOFIX_CONSECUTIVE_POLLS = 2;   // ~30s of a steady fault, not one flap
    const int AUTOFIX_MAX_ATTEMPTS      = 3;   // then it is the human's turn
    // 2026-09-24: the window AUTOFIX_MAX_ATTEMPTS applies over. This used to be an unbounded
    // process lifetime -- which was fine as long as the process lived, and meant nothing at all
    // once the loop this file's startup-gate comment describes made "the process" mean a few
    // seconds. 30 minutes bounds a genuinely stuck repair without permanently locking one out.
    const double AUTOFIX_BUDGET_WINDOW_S = 1800.0;
    const int AUTOFIX_GREEN_POLLS_TO_RESET = 3; // one green poll is a flap, not a recovery
    int _autoFixBadPolls = 0;
    int _autoFixGreenPolls = 0;
    string _autoFixFault = "";
    int _autoFixAttempts = 0;
    // Which repair keys this PROCESS has already told the operator gave up, so a persisted
    // exhaustion (bug (b): a fresh process used to get a fresh budget and a fresh silence)
    // still surfaces the "not a loop" message once here, without repeating it every ~15s poll
    // for as long as the fault and the exhaustion both persist.
    readonly HashSet<string> _autoFixExhaustedNoted = new HashSet<string>();

    // Which dot a repair would target, and whether that repair touches the FLEET's Edge.
    //
    // The first version stood down for ANY live run, which silenced two things it should not
    // have. The tool dot's repair targets the interactive bridge on :9223 -- a different
    // browser -- so a long fleet run could leave the chat side broken indefinitely. And the
    // agent dot is only ever red or yellow WHILE a run is live, so gating on "a run is live"
    // made automatic agent repair unreachable by construction.
    //
    // The question is not "is something running" but "does this repair touch what is running".
    // The highest-priority fault THAT CAN BE ACTED ON RIGHT NOW.
    //
    // Returning the highest-priority fault outright was wrong, and it showed up the first time
    // the incident reproduced: a live run made the agent dot red, agent repair touches the
    // fleet's Edge so it stood down, and the tool dot -- red, repairable, and pointed at a
    // DIFFERENT browser (:9223) -- was never reached. The chat side stayed broken because the
    // fleet side was busy. A fault that cannot be repaired now must not shadow one that can.
    int AutoFixTargetDot()
    {
        bool runLive = FleetRunIsLive();
        int[] priority = { 3, 2, 4, 0, 1, 5 };   // sign-in, edge, agent, server, tunnel, tool
        lock (_healthLock)
        {
            foreach (int dot in priority)
            {
                HealthState st = _health[dot].State;
                bool bad = (st == HealthState.Red)
                        || (st == HealthState.Yellow && (dot == 2 || dot == 4 || dot == 5));
                if (!bad) continue;
                // RunFix has a sign-in branch for RED only; a yellow sign-in would burn the
                // whole budget doing nothing, so it is not a target.
                // AND NEVER AN AUTOMATIC TARGET EVEN WHEN RED: the sign-in repair relaunches the
                // fleet's Edge WITH A WINDOW (start_companion_edge.ps1 -Foreground). That is for
                // a person who pressed the Fix button; an unattended auto-fix opening a visible
                // browser mid-run is the incident of 2026-10-08. Red sign-in stays on screen for
                // the person to act on.
                if (dot == 3) continue;
                if (RepairTouchesFleetEdge(dot) && runLive) continue;   // try the next one
                return dot;
            }
        }
        return -1;
    }

    // WHICH REPAIR A RED DOT LEADS TO, so the retry budget can be charged to the repair rather
    // than to the observation. The mapping is RunFix's own branch structure, read off it:
    //
    //   3 sign-in  -> start_companion_edge.ps1 -Foreground   (Priority 1)
    //   2 edge     -> start_companion_edge.ps1 -HardReset    (Priority 1b/2)
    //   4 agent    -> RunReconnect                           (Priority 3)
    //   0 server   -\
    //   1 tunnel   -/ scripts\repair.ps1 -Auto               (Priority 4, ONE branch)
    //   5 tool     -> its own tier
    //
    // ONLY 0 AND 1 ARE MERGED, and only because RunFix merges them in a single `if`. sign-in and
    // edge both relaunch :9222 and might look mergeable, but they take different branches with
    // different arguments -- merging them from that resemblance would be inference, and the one
    // thing this function must not do is guess which repairs are the same.
    static string AutoFixRepairKey(int dot)
    {
        if (dot == 0 || dot == 1) return "repair:stack";
        return "repair:dot" + dot;
    }

    static bool RepairTouchesFleetEdge(int dot)
    {
        // sign-in and edge relaunch :9222; agent reconnects it. tool targets :9223, and
        // server/tunnel restart the backend -- neither is the fleet's browser.
        return dot == 3 || dot == 2 || dot == 4;
    }

    // Re-checks the live-run guard on the UI thread immediately before acting: MaybeAutoFix
    // ran on the poll thread, and a run can start in the gap between that check and this call.
    void RunFixAuto(int dot)
    {
        if (RepairTouchesFleetEdge(dot) && FleetRunIsLive())
        {
            AutoFixRecord(dot, "skipped: a run started between the poll and this call");
            return;
        }
        AutoFixRecord(dot, "running");
        RunFix();
    }

    // A DURABLE TRACE, because RunFix only ever wrote to a label in this window.
    //
    // An automatic repair that leaves no record cannot be audited: if it fires at three in the
    // morning and makes things worse, there is nothing to look at, and "did it even run?" is
    // unanswerable. I could not answer that question about its first real firing, which is how
    // UTF-8 WITHOUT THE PREAMBLE, WHICH Encoding.UTF8 IS NOT. .NET's Encoding.UTF8 emits a
    // BOM, and File.AppendAllText writes it when it creates the file -- so the FIRST line of
    // every ledger this cockpit starts carries three bytes no line-oriented reader expects.
    // Measured 2026-09-22: .fleet/autofix.jsonl and .fleet/ui_errors.jsonl both begin with
    // one, and line 1 of autofix.jsonl is the single row in that file that json.loads
    // refuses ("Unexpected UTF-8 BOM (decode using utf-8-sig)"). One unreadable row in five
    // hundred is the kind of thing that gets called a torn write and is not.
    //
    // The rule this repository already holds is write no-BOM, read utf-8-sig; the write half
    // was missing here. Shared so the next ledger written from this file cannot pick the
    // wrong one -- the same reason ui/FleetCommands.cs exists.
    static readonly Encoding NoBomUtf8 = new UTF8Encoding(false);

    // this line came to exist.
    void AutoFixRecord(int dot, string what)
    {
        try
        {
            string[] names = { "server", "tunnel", "edge", "signin", "agent", "tool" };
            string name = (dot >= 0 && dot < names.Length) ? names[dot] : ("dot" + dot);
            string detail;
            lock (_healthLock) { detail = _health[dot >= 0 ? dot : 0].Detail ?? ""; }
            string line = "{\"ts\":" + DateTimeOffset.UtcNow.ToUnixTimeSeconds()
                        + ",\"dot\":\"" + name + "\""
                        + ",\"attempt\":" + _autoFixAttempts
                        + ",\"what\":\"" + what.Replace("\"", "'") + "\""
                        + ",\"detail\":\"" + detail.Replace("\\", "/").Replace("\"", "'") + "\"}";
            string path = Path.Combine(Path.GetDirectoryName(ResolvePath(null)), "autofix.jsonl");
            File.AppendAllText(path, line + Environment.NewLine, NoBomUtf8);
        }
        catch (Exception) { }   // a trace that can break the repair is worse than no trace
    }

    // ── cross-process gates for automatic startup/repair (2026-09-24 startup-loop fix) ──────
    //
    // Whether start_all.ps1 is CURRENTLY mid-bring-up on this machine, checked the same way its
    // own Enter-StartAllLock does: the "Global\m365-copilot-companion-start-all" named mutex.
    // WaitOne(0) is a non-blocking probe -- acquire-and-immediately-release if free, so this
    // never itself waits the up-to-600s a real start_all launch would. An abandoned mutex (the
    // holder died) is taken as "not running", same interpretation start_all.ps1's own comment
    // gives it ("still handed over ownership... rather than treated as a failure"). A missing
    // lock (no OS support) must never be mistaken for "running forever", so any exception here
    // also reads as "not running" -- the same fail-open start_all.ps1 itself uses.
    static bool IsStartAllRunning()
    {
        try
        {
            using (var m = new Mutex(false, @"Global\m365-copilot-companion-start-all"))
            {
                try
                {
                    if (m.WaitOne(0)) { m.ReleaseMutex(); return false; }
                    return true;
                }
                catch (AbandonedMutexException)
                {
                    try { m.ReleaseMutex(); } catch (Exception) { }
                    return false;
                }
            }
        }
        catch (Exception) { return false; }
    }

    // scripts/start_all.ps1 sets M365_LAUNCHED_BY_START_ALL=1 in the environment of ONLY the UI
    // process it launches (see its "THE WINDOW MAY ASK 'WHO OPENED ME?'" comment) -- this reads
    // that flag. No start_all.ps1 change needed; the flag already existed and was unread here.
    static bool LaunchedByStartAll()
    {
        try { return Environment.GetEnvironmentVariable("M365_LAUNCHED_BY_START_ALL") == "1"; }
        catch (Exception) { return false; }
    }

    const string AUTOFIX_BUDGET_FILENAME = "autofix_budget.json";

    static string AutoFixBudgetPath()
    {
        return Path.Combine(Path.GetDirectoryName(ResolvePath(null)), AUTOFIX_BUDGET_FILENAME);
    }

    // Best-effort read of {"<key>": [ts, ts, ...], ...}. Any I/O/parse failure (missing file,
    // torn write, a hand-edited file) reads as "no history for any key" -- the safe direction,
    // since it costs one extra attempt rather than silencing repair for ever.
    Dictionary<string, object> LoadAutoFixBudgetRaw()
    {
        try
        {
            string path = AutoFixBudgetPath();
            if (!File.Exists(path)) return new Dictionary<string, object>();
            var d = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
            return d ?? new Dictionary<string, object>();
        }
        catch (Exception) { return new Dictionary<string, object>(); }
    }

    static List<double> AttemptTimestampsFor(Dictionary<string, object> data, string key)
    {
        var list = new List<double>();
        object v;
        if (data != null && data.TryGetValue(key, out v) && v is object[])
            foreach (object o in (object[])v)
                try { list.Add(Convert.ToDouble(o, CultureInfo.InvariantCulture)); }
                catch (Exception) { }
        return list;
    }

    // tmp+delete+move, the same atomic-write shape PublishHealthStrip already uses for its own
    // file in this same directory.
    void SaveAutoFixBudgetRaw(Dictionary<string, object> data)
    {
        try
        {
            string path = AutoFixBudgetPath();
            string tmp = path + ".tmp";
            File.WriteAllText(tmp, _js.Serialize(data), NoBomUtf8);
            if (File.Exists(path)) File.Delete(path);
            File.Move(tmp, path);
        }
        catch (Exception) { }
    }

    // Read-decide-write in one call: the ONE place FleetCockpit.cs touches the budget file, so
    // every automatic launch (RunStartAll's own cooldown, and each MaybeAutoFix repair key)
    // shares one persistence path rather than growing a second. Cross-process races (two
    // cockpits reading, deciding, and writing back within the same instant) are not locked
    // against here -- the same risk PublishHealthStrip already accepts for this directory --
    // but the failure mode of a lost race is "one extra attempt slips through the cap", not the
    // unbounded-relaunch loop this exists to fix.
    bool TryConsumeAutoFixBudget(string key, int maxAttempts, double windowS, out bool exhausted)
    {
        var data = LoadAutoFixBudgetRaw();
        var attempts = AttemptTimestampsFor(data, key);
        AutoFixBudget.Decision d = AutoFixBudget.Decide(attempts, NowUnix(), maxAttempts, windowS);
        data[key] = d.Kept.ToArray();
        SaveAutoFixBudgetRaw(data);
        exhausted = d.Exhausted;
        return d.Allow;
    }

    // Marshal a note onto the UI thread from wherever (poll thread or UI thread alike),
    // mirroring the Dispatcher pattern the startup auto-heal block already used inline.
    void NoteFromAnyThread(string text)
    {
        try
        {
            if (!Dispatcher.HasShutdownStarted)
                Dispatcher.BeginInvoke(new Action(delegate { if (_fixNote != null) _fixNote.Text = text; }));
        }
        catch (Exception) { }
    }

    // ONE COCKPIT REPAIRS. Nothing stops a second one being opened, and each process has its
    // own _fixRunning and its own attempt budget -- so two of them would restart the same
    // browser twice, each believing it was the only one. The mutex is held for the process's
    // life; whichever cockpit gets it does the repairs and the others only display.
    static Mutex _autoFixMutex;
    static bool _autoFixIsMine;
    static bool _autoFixMutexChecked;

    static bool ThisCockpitOwnsAutoFix()
    {
        if (!_autoFixMutexChecked)
        {
            _autoFixMutexChecked = true;
            try
            {
                bool createdNew;
                _autoFixMutex = new Mutex(true, @"Local\M365CompanionAutoFix", out createdNew);
                _autoFixIsMine = createdNew;
            }
            catch (Exception) { _autoFixIsMine = false; }
        }
        return _autoFixIsMine;
    }

    void MaybeAutoFix()
    {
        if (!ThisCockpitOwnsAutoFix()) return;
        int dot = AutoFixTargetDot();
        if (dot < 0)
        {
            // Count green polls rather than resetting on the first: a fault that flaps green
            // for one tick would otherwise get an unlimited budget and restart for ever.
            _autoFixGreenPolls++;
            if (_autoFixGreenPolls >= AUTOFIX_GREEN_POLLS_TO_RESET)
            {
                _autoFixBadPolls = 0;
                _autoFixAttempts = 0;
                _autoFixFault = "";
                _autoFixExhaustedNoted.Clear();
            }
            return;
        }
        _autoFixGreenPolls = 0;

        // A DIFFERENT FAULT GETS ITS OWN BUDGET. One persistent high-priority failure used to
        // consume all three attempts and leave every later fault unrepaired.
        //
        // AND THE BUDGET BELONGS TO THE REPAIR, NOT THE DOT -- which is what that earlier fix
        // got wrong, in a way that made AUTOFIX_MAX_ATTEMPTS bound nothing at all. RunFix
        // handles `server == Red || tunnel == Red` in ONE branch (Priority 4, scripts\repair.ps1
        // -Auto), so those two dots are the same repair observed from two probes: any cause that
        // takes the backend down reddens both, and they flap. Every flip changed `fault`, which
        // reset _autoFixAttempts to 0, which handed out a fresh budget of three. Measured in
        // .fleet/autofix.jsonl on 2026-09-14, 07:49-07:51 JST:
        //
        //     tunnel#1  tunnel#2  server#1  tunnel#1  tunnel#2  tunnel#3
        //                                   ^ the cap was reached and then reset
        //
        // -- repair.ps1 relaunched every ~186s (its own run time plus the post-repair
        // _healthWake re-poll) with no bound in sight. Keying on the repair collapses that pair
        // into one budget while leaving genuinely different repairs their own, which is what the
        // paragraph above was for.
        string fault = AutoFixRepairKey(dot);
        if (fault != _autoFixFault)
        {
            _autoFixFault = fault;
            _autoFixAttempts = 0;
            _autoFixBadPolls = 0;
        }

        _autoFixBadPolls++;
        if (_autoFixBadPolls < AUTOFIX_CONSECUTIVE_POLLS) return;
        if (_fixRunning) return;

        // PERSISTED, not per-process (2026-09-24 startup-loop fix): AUTOFIX_MAX_ATTEMPTS used
        // to bound only _autoFixAttempts, a field of THIS process. A cockpit relaunched by the
        // very loop this budget exists to stop got a brand-new field, and so a brand-new three
        // tries, every time -- the budget bound nothing across the relaunches that mattered
        // most. TryConsumeAutoFixBudget reads/writes .fleet/autofix_budget.json instead.
        bool exhausted;
        if (!TryConsumeAutoFixBudget(fault, AUTOFIX_MAX_ATTEMPTS, AUTOFIX_BUDGET_WINDOW_S, out exhausted))
        {
            if (exhausted && !_autoFixExhaustedNoted.Contains(fault))
            {
                _autoFixExhaustedNoted.Add(fault);
                AutoFixRecord(dot, "exhausted: automatic repair stopped retrying " + fault);
                NoteFromAnyThread(T("autofix_exhausted"));
            }
            return;   // the button is the way on
        }

        _autoFixAttempts++;
        _autoFixBadPolls = 0;
        try
        {
            int d = dot;
            if (!Dispatcher.HasShutdownStarted)
                Dispatcher.BeginInvoke(new Action(delegate { RunFixAuto(d); }));
        }
        catch (Exception) { }
    }
}
