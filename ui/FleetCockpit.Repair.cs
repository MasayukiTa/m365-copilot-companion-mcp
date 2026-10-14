// FleetCockpit.Repair.cs -- the Fix button: RunFix, bridge reconnect, repair dispatcher, RunStartAll.
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

    // ── Fix button: run the remedy for the WORST current problem ─────────────────────
    // Priority (worst first): sign-in RED -> Edge RED -> agent RED/YELLOW -> server RED.
    // Each remedy is a short-lived, windowless, async shell; never blocks the UI; guarded.
    // Never two at once (button disabled while running).
    void RunFix()
    {
        if (_fixRunning) return;
        // Decide the worst problem from the current cache (UI thread).
        HealthState signin, edge, agent, server, tunnel, tool;
        lock (_healthLock)
        {
            server = _health[0].State; tunnel = _health[1].State; edge = _health[2].State;
            signin = _health[3].State; agent = _health[4].State; tool = _health[5].State;
        }
        if (signin == HealthState.Red) _fixTargetMask = 1 << 3;
        else if (edge == HealthState.Red) _fixTargetMask = 1 << 2;
        else if (agent == HealthState.Yellow || agent == HealthState.Red) _fixTargetMask = 1 << 4;
        else if (server == HealthState.Red || tunnel == HealthState.Red)
            _fixTargetMask = ((server == HealthState.Red) ? (1 << 0) : 0)
                           | ((tunnel == HealthState.Red) ? (1 << 1) : 0);
        else if (tool == HealthState.Red || tool == HealthState.Yellow) _fixTargetMask = 1 << 5;
        else _fixTargetMask = 0;
        _fixRunning = true;
        ApplyHealthToUi();   // disable + keep the button visible
        if (_fixNote != null) _fixNote.Text = T("hs_fixing");

        string repo = RepoRoot();
        Action<string> note = delegate (string s)
        {
            try { if (!Dispatcher.HasShutdownStarted) Dispatcher.BeginInvoke(new Action(delegate { if (_fixNote != null) _fixNote.Text = s; })); }
            catch (Exception) { }
        };
        Action done = delegate
        {
            _fixRunning = false;
            _fixTargetMask = 0;
            try { _healthWake.Set(); } catch (Exception) { }
            try { if (!Dispatcher.HasShutdownStarted) Dispatcher.BeginInvoke(new Action(delegate { ApplyHealthToUi(); })); } catch (Exception) { }
        };

        // Priority 1: Sign-in RED -> relaunch companion Edge HEADED for the user to sign in.
        if (signin == HealthState.Red)
        {
            note(T("hs_fix_signin"));
            var t = new Thread(new ThreadStart(delegate
            {
                try
                {
                    RunPowershellScript(Path.Combine(repo, "scripts", "start_companion_edge.ps1"),
                                        "-Foreground -Port 9222");
                    note(T("hs_fix_signin_toast"));
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); }
                finally { done(); }
            })) { IsBackground = true };
            t.Start();
            return;
        }

        // Priority 1b: Edge YELLOW (browser up, no agent page) -> navigate. Do NOT relaunch.
        //
        // This is the incident state, and the repair for it is one navigation. Measured by
        // hand on 2026-08-31: eight seconds. A hard reset would also work and would throw away
        // a browser that is fine, taking the session and any warm state with it -- the heavier
        // remedy is for an Edge that does not answer at all, which is Priority 2 below.
        if (edge == HealthState.Yellow)
        {
            note(T("hs_fix_edge_navigate"));
            var tnav = new Thread(new ThreadStart(delegate
            {
                try
                {
                    string agentUrl = EnvValue("MCP_FLEET_AGENT_URL");
                    if (string.IsNullOrEmpty(agentUrl)) agentUrl = EnvValue("MCP_IMPL_AGENT_URL");
                    if (string.IsNullOrEmpty(agentUrl)) { note(T("hs_fix_err")); return; }
                    // relay.edge_auth --ensure <url> exists so this caller does not have to
                    // invent its own way to run an inline snippet.
                    string outp = RunPyModule("relay.edge_auth",
                                              "--ensure \"" + agentUrl + "\"", 180000);
                    note((outp ?? "").Trim() == "ready" ? T("hs_fix_done") : T("hs_fix_edge_still"));
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); }
                finally { done(); }
            })) { IsBackground = true };
            tnav.Start();
            return;
        }

        // Priority 2: Edge RED -> hard-reset relaunch (preserves remembered headless/headed mode).
        if (edge == HealthState.Red)
        {
            note(T("hs_fix_edge"));
            var t = new Thread(new ThreadStart(delegate
            {
                try
                {
                    RunPowershellScript(Path.Combine(repo, "scripts", "start_companion_edge.ps1"),
                                        "-HardReset -Port 9222");
                    note(T("hs_fix_done"));
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); }
                finally { done(); }
            })) { IsBackground = true };
            t.Start();
            return;
        }

        // Priority 3: Agent RED/YELLOW (missing chat / default-Copilot fallback) -> edge_reconnect.
        if (agent == HealthState.Yellow || agent == HealthState.Red)
        {
            note(T("hs_fix_agent"));
            var t = new Thread(new ThreadStart(delegate
            {
                try
                {
                    int code = RunReconnect(repo);
                    note(code == 0 ? T("hs_fix_agent_ok") : T("hs_fix_agent_fail"));
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); }
                finally { done(); }
            })) { IsBackground = true };
            t.Start();
            return;
        }

        // Priority 4: Server RED or Tunnel RED -> route through the situation-aware repair
        // dispatcher (scripts\repair.ps1 -Auto -ResultJson) instead of blindly launching the
        // stack bring-up. doctor.ps1's layered Dev Tunnel diagnosis (and repair.ps1's tiers
        // built on it) know the difference between "server/tunnel just need starting" (Tier A,
        // fixed by start_all -- repair.ps1 runs this itself under -Auto) and "the devtunnel CLI
        // login expired" (Tier C, human-only -- start_all can NEVER fix this, so the old blind
        // RunStartAll() here used to do nothing useful and leave the user stuck). Run off the
        // UI thread like the other async tiers above so a slow repair pass cannot freeze the UI.
        if (server == HealthState.Red || tunnel == HealthState.Red)
        {
            // ANOTHER PC IS SERVING THIS TUNNEL (state foreign/shared, scripts/supervisor.ps1,
            // commit 57ad0d1) -- and if that is the ONLY reason this branch fired (server is
            // fine), repair.ps1 has nothing to repair. Its Tier A for the tunnel IS start_all,
            // which brings up THIS machine's own stack; it cannot make a different PC stop
            // answering for this one. Running it anyway would not fix anything -- it would
            // relaunch start_all every autofix cycle against a condition it structurally cannot
            // change, spending the auto-fix retry budget (AUTOFIX_MAX_ATTEMPTS) on a loop with no
            // exit. Tell the operator the supervisor's own diagnosis instead, the same way the
            // Tier C (human-only, e.g. devtunnel login) branch below already does.
            TunnelHostState tunHost = ReadTunnelHostState();
            bool tunnelHijacked = tunnel == HealthState.Red && server != HealthState.Red
                                 && tunHost != null
                                 && (tunHost.State == "foreign" || tunHost.State == "shared");
            if (tunnelHijacked)
            {
                string msg = tunHost.Message;
                if (!string.IsNullOrEmpty(tunHost.Action)) msg = msg + "  " + tunHost.Action;
                note(string.IsNullOrEmpty(msg) ? T("hs_tun_detail_bad") : msg);
                done();
                return;
            }
            note(T("hs_fix_stack"));
            var t = new Thread(new ThreadStart(delegate
            {
                try
                {
                    // repair.ps1's Tier A for server/tunnel IS another start_all.ps1 invocation
                    // (`-NoUi -NoSplash`) -- running it while one is already mid-bring-up does
                    // not fix anything, it queues a second startup behind the first one's lock.
                    // Same check RunStartAll() makes itself, made here too because this branch
                    // can reach repair.ps1 without ever calling RunStartAll directly.
                    if (IsStartAllRunning()) { note(T("autofix_start_all_running")); return; }
                    string repairPs1 = Path.Combine(repo, "scripts", "repair.ps1");
                    RepairResult rr = File.Exists(repairPs1) ? ParseRepairResult(RunRepairDispatcher(repairPs1)) : null;
                    if (rr == null)
                    {
                        // repair.ps1 missing, failed to run, or its output could not be parsed.
                        // THIS USED TO fall back to a blind RunStartAll() "to never regress" --
                        // measured (2026-09-24, .fleet/autofix.jsonl + process trees, 07:35-
                        // 07:53) to be the third way this exact branch could add another queued
                        // startup on top of an already-running one, with nothing to show for it
                        // when repair.ps1's own diagnosis was simply unreadable. Record it and
                        // tell the operator instead of guessing at a fix.
                        AutoFixRecord(0, "repair.ps1 output unparsable or unavailable; not "
                                       + "falling back to a blind start_all");
                        note(T("hs_fix_repair_unreadable"));
                    }
                    else if (rr.HumanSteps.Count > 0)
                    {
                        // The key win: tell the user the exact manual step (e.g. devtunnel login)
                        // instead of silently re-running start_all, which cannot fix a Tier C cause.
                        note(T("hs_fix_manual_needed") + ":  " + string.Join("   /   ", rr.HumanSteps.ToArray()));
                    }
                    else if (rr.FinalBad == 0)
                    {
                        note(T("hs_fix_done"));
                    }
                    else
                    {
                        note(T("hs_fix_stack"));
                    }
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); RunStartAll(); }
                finally { done(); }
            })) { IsBackground = true };
            t.Start();
            return;
        }

        // Priority 5: Tool RED/YELLOW (bridge-side problem -- the interactive chat's own MCP
        // connector, NOT the fleet Edge above) -> reconnect the BRIDGE specifically, i.e. target
        // :9223 (copilot-bridge-edge) instead of the :9222 fleet Edge Priority 3 above already
        // handles. This is a SEPARATE remedy path (:9222 reconnect above is untouched/still runs
        // for its own condition) because a healthy fleet Edge tells you nothing about the bridge.
        if (tool == HealthState.Red || tool == HealthState.Yellow)
        {
            note(T("hs_fix_bridge"));
            var t = new Thread(new ThreadStart(delegate
            {
                try
                {
                    string stdoutText;
                    int code = RunReconnect(repo, "http://127.0.0.1:9223", out stdoutText);
                    if (code == 0) note(T("hs_fix_bridge_ok"));
                    else if (AgentDidNotLoad(stdoutText)) note(T("reconnect_chat_toast_dead"));
                    else note(T("hs_fix_bridge_fail"));
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); }
                finally { done(); }
            })) { IsBackground = true };
            t.Start();
            return;
        }

        // Nothing actionable (all green/gray) -> clear the running flag.
        done();
    }

    // ── Manual "チャット再接続"/"Reconnect chat" button (settings panel) ──────────────────
    // ALWAYS available, independent of the Tool dot's state (which may be GRAY on a machine
    // where the bridge self-probe feature isn't active yet, or the user may just want to force
    // it). Fires the same bridge-targeted (:9223) reconnect as RunFix's Priority 5 tier, but on
    // demand. Exception-guarded throughout; every UI touch is Dispatcher-marshaled so nothing can
    // throw into the UI thread. Reuses ShowScaleToast -- the cockpit's existing lightweight toast
    // -- for the optimistic "reconnecting…" message and the outcome (mirrors the steer-ack toast
    // pattern at "steer_collapsed_ack").
    /// Tint the manual reconnect control by the Tool dot -- amber only when a reconnect is
    /// actually indicated (dot Yellow or Red), muted otherwise.
    ///
    /// GRAY IS MUTED, NOT AMBER. Gray means the probe has no opinion (it has not run, or this
    /// machine does not run it). "No opinion" is not "something is wrong", and painting it amber
    /// is how the control came to be permanently lit in the first place.
    void RefreshReconnectChatTint()
    {
        if (_reconnectChatBtn == null) return;
        HealthState tool;
        lock (_healthLock) { tool = _health[5].State; }
        bool needed = tool == HealthState.Yellow || tool == HealthState.Red;
        var tint = needed ? Theme.Br(Theme.Warning(_dark)) : Muted;
        _reconnectChatBtn.Foreground = tint;
        _reconnectChatBtn.BorderBrush = tint;
    }

    void RunBridgeReconnectManual()
    {
        if (_bridgeReconnectRunning) return;
        _bridgeReconnectRunning = true;
        if (_reconnectChatBtn != null) _reconnectChatBtn.IsEnabled = false;
        try { ShowScaleToast(T("reconnect_chat_toast_start")); } catch (Exception) { }

        string repo = RepoRoot();
        var t = new Thread(new ThreadStart(delegate
        {
            string outcome;
            try
            {
                string stdoutText;
                int code = RunReconnect(repo, "http://127.0.0.1:9223", out stdoutText);
                outcome = code == 0 ? T("reconnect_chat_toast_ok")
                        : AgentDidNotLoad(stdoutText) ? T("reconnect_chat_toast_dead")
                        : T("reconnect_chat_toast_fail");
            }
            catch (Exception) { outcome = T("reconnect_chat_toast_fail"); }
            try
            {
                if (!Dispatcher.HasShutdownStarted)
                    Dispatcher.BeginInvoke(new Action(delegate
                    {
                        _bridgeReconnectRunning = false;
                        if (_reconnectChatBtn != null) _reconnectChatBtn.IsEnabled = true;
                        try { ShowScaleToast(outcome); } catch (Exception) { }
                    }));
                else
                    _bridgeReconnectRunning = false;
            }
            catch (Exception) { _bridgeReconnectRunning = false; }
        })) { IsBackground = true };
        t.Start();
    }

    // Launch a PowerShell script windowless + async; wait for exit inside the caller's worker thread.
    void RunPowershellScript(string scriptPath, string extraArgs)
    {
        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = "powershell";
        psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + scriptPath + "\" " + extraArgs;
        psi.WorkingDirectory = RepoRoot();
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        using (var p = System.Diagnostics.Process.Start(psi))
        {
            // BOTH STREAMS AT ONCE, AND A TIMEOUT THAT ACTUALLY BOUNDS THE CALL.
            //
            // Draining stdout to completion before touching stderr deadlocks if the child
            // fills the stderr pipe: it blocks writing, so it never closes stdout, so the
            // reader never returns -- and WaitForExit, the only bounded step, is never
            // reached. Python writes to stderr here on every call (import-time deprecation
            // warnings), so that buffer is never empty. This pattern was fixed once in
            // RunPyModule and left in place at three other call sites; a wedge here leaves
            // _fixRunning true for ever, which disables the automatic repair AND the button.
            p.OutputDataReceived += delegate (object _s, System.Diagnostics.DataReceivedEventArgs e) { };
            p.ErrorDataReceived += delegate (object _s, System.Diagnostics.DataReceivedEventArgs e) { };
            try { p.BeginOutputReadLine(); p.BeginErrorReadLine(); } catch (Exception) { }
            bool exited = false;
            try { exited = p.WaitForExit(120000); } catch (Exception) { }
            if (!exited) { try { p.Kill(); } catch (Exception) { } }
        }
    }

    // edge_reconnect.py prints this exact marker (main(), the "not res.get('agent_loaded')" branch)
    // when the deep-link fell back to default Copilot -- a heavier remedy (start_bridge.ps1
    // -HardReset) is needed, a plain reconnect can't fix it. Checked against captured stdout.
    static bool AgentDidNotLoad(string stdoutText)
    {
        return !string.IsNullOrEmpty(stdoutText)
            && stdoutText.IndexOf("AGENT DID NOT LOAD", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    // Run  <repo>\.venv\Scripts\python.exe -m relay.edge_reconnect  and capture the exit code.
    // Original (fleet Edge :9222, default inside edge_reconnect.py) call shape -- unchanged, still
    // used by RunFix's Priority 3 (Agent YELLOW) tier above.
    int RunReconnect(string repo)
    {
        string dump;
        return RunReconnect(repo, null, out dump);
    }

    // Same as above but targets a SPECIFIC CDP endpoint (--cdp-url) instead of edge_reconnect.py's
    // :9222 default, and returns the captured stdout so callers can look for edge_reconnect.py's
    // "AGENT DID NOT LOAD" marker (printed when the deep-link fell back to default Copilot --
    // the case where a lighter reconnect can't help and start_bridge.ps1 -HardReset is needed).
    // cdpUrl == null keeps edge_reconnect.py's own default (:9222); pass "http://127.0.0.1:9223"
    // to target the interactive BRIDGE profile instead.
    int RunReconnect(string repo, string cdpUrl, out string stdoutText)
    {
        string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
        if (!File.Exists(py)) py = "python";
        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = py;
        psi.Arguments = "-m relay.edge_reconnect" + (string.IsNullOrEmpty(cdpUrl) ? "" : " --cdp-url " + cdpUrl);
        psi.WorkingDirectory = repo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
        stdoutText = "";
        var sbOut = new StringBuilder();
        using (var p = System.Diagnostics.Process.Start(psi))
        {
            // BOTH STREAMS AT ONCE, AND A TIMEOUT THAT ACTUALLY BOUNDS THE CALL.
            //
            // Draining stdout to completion before touching stderr deadlocks if the child
            // fills the stderr pipe: it blocks writing, so it never closes stdout, so the
            // reader never returns -- and WaitForExit, the only bounded step, is never
            // reached. Python writes to stderr here on every call (import-time deprecation
            // warnings), so that buffer is never empty. This pattern was fixed once in
            // RunPyModule and left in place at three other call sites; a wedge here leaves
            // _fixRunning true for ever, which disables the automatic repair AND the button.
            p.OutputDataReceived += delegate (object _s, System.Diagnostics.DataReceivedEventArgs e)
            { if (e.Data != null) lock (sbOut) sbOut.AppendLine(e.Data); };
            p.ErrorDataReceived += delegate (object _s, System.Diagnostics.DataReceivedEventArgs e) { };
            try { p.BeginOutputReadLine(); p.BeginErrorReadLine(); } catch (Exception) { }
            bool exited = false;
            try { exited = p.WaitForExit(600000); } catch (Exception) { }
            lock (sbOut) stdoutText = sbOut.ToString();
            if (!exited) { try { p.Kill(); } catch (Exception) { } return -1; }
            try { return p.ExitCode; } catch (Exception) { return -1; }
        }
    }

    // Parsed shape of scripts\repair.ps1 -ResultJson's final JSON line:
    //   { autofixed:[{id,note}], confirmNeeded:[{id,note}], humanSteps:[{id,step}],
    //     finalOk:<int>, finalBad:<int> }
    // RunFix's Priority 4 tier only needs HumanSteps (to surface the manual step, e.g.
    // "devtunnel login") and FinalBad (to tell "fixed" from "still broken, no human step
    // known") -- autofixed/confirmNeeded are part of the JSON but not consumed here.
    class RepairResult
    {
        public List<string> HumanSteps = new List<string>();
        public int FinalOk;
        public int FinalBad;
    }

    // Runs  scripts\repair.ps1 -Auto -ResultJson  windowless and returns its captured stdout.
    // -Auto: Tier A repairs (e.g. start the stack) run automatically; Tier B (install/rebuild)
    // is skipped, not attempted unattended; Tier C (human-only, e.g. devtunnel login) is only
    // ever printed, never attempted. Same ProcessStartInfo shape as RunPowershellScript/
    // RunReconnect above; up to ~2 min for the stack bring-up, same budget RunStartAll assumes.
    string RunRepairDispatcher(string repairPs1)
    {
        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = "powershell";
        psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + repairPs1 + "\" -Auto -ResultJson";
        psi.WorkingDirectory = RepoRoot();
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        string stdoutText = "";
        var sbOut3 = new StringBuilder();
        using (var p = System.Diagnostics.Process.Start(psi))
        {
            // BOTH STREAMS AT ONCE, AND A TIMEOUT THAT ACTUALLY BOUNDS THE CALL.
            //
            // Draining stdout to completion before touching stderr deadlocks if the child
            // fills the stderr pipe: it blocks writing, so it never closes stdout, so the
            // reader never returns -- and WaitForExit, the only bounded step, is never
            // reached. Python writes to stderr here on every call (import-time deprecation
            // warnings), so that buffer is never empty. This pattern was fixed once in
            // RunPyModule and left in place at three other call sites; a wedge here leaves
            // _fixRunning true for ever, which disables the automatic repair AND the button.
            p.OutputDataReceived += delegate (object _s, System.Diagnostics.DataReceivedEventArgs e)
            { if (e.Data != null) lock (sbOut3) sbOut3.AppendLine(e.Data); };
            p.ErrorDataReceived += delegate (object _s, System.Diagnostics.DataReceivedEventArgs e) { };
            try { p.BeginOutputReadLine(); p.BeginErrorReadLine(); } catch (Exception) { }
            bool exited = false;
            try { exited = p.WaitForExit(180000); } catch (Exception) { }
            lock (sbOut3) stdoutText = sbOut3.ToString();
            if (!exited) { try { p.Kill(); } catch (Exception) { } }
        }
        return stdoutText;
    }

    // Parses repair.ps1 -ResultJson's LAST non-empty stdout line as the compact JSON summary
    // (every earlier line is repair.ps1's normal human-readable progress text). Returns null
    // (never throws) if nothing parseable was produced -- the caller then falls back to the
    // previous blind RunStartAll() behavior so a missing/broken repair.ps1 can never regress
    // the Fix button. Reuses the file's existing _js (JavaScriptSerializer) instance.
    RepairResult ParseRepairResult(string stdoutText)
    {
        try
        {
            if (string.IsNullOrEmpty(stdoutText)) return null;
            string[] lines = stdoutText.Replace("\r\n", "\n").Split('\n');
            string lastLine = null;
            for (int i = lines.Length - 1; i >= 0; i--)
            {
                if (!string.IsNullOrEmpty(lines[i].Trim())) { lastLine = lines[i].Trim(); break; }
            }
            if (lastLine == null) return null;
            var d = _js.DeserializeObject(lastLine) as Dictionary<string, object>;
            if (d == null) return null;

            var rr = new RepairResult();
            object hs;
            if (d.TryGetValue("humanSteps", out hs) && hs is object[])
            {
                foreach (var item in (object[])hs)
                {
                    var m = item as Dictionary<string, object>;
                    object step;
                    if (m != null && m.TryGetValue("step", out step) && step != null)
                        rr.HumanSteps.Add(Convert.ToString(step));
                }
            }
            object fo, fb;
            rr.FinalOk  = d.TryGetValue("finalOk", out fo)  ? Convert.ToInt32(fo)  : 0;
            rr.FinalBad = d.TryGetValue("finalBad", out fb) ? Convert.ToInt32(fb) : 0;
            return rr;
        }
        catch (Exception) { return null; }
    }

    const double START_ALL_COOLDOWN_S = 120.0;

    // Fire the full stack bring-up EXACTLY the way the desktop icon does: wscript.exe running
    // start_all_hidden.vbs, which in turn drives scripts\start_all.ps1 (Invoke-Startup starts
    // supervisor.ps1 [MCP server + devtunnel], companion Edge, bridge, UIs). start_all.ps1 is
    // idempotent -- it skips components already running -- so calling this when the stack is
    // already healthy is a safe no-op. Fire-and-forget: we do not wait for it to finish (up to
    // ~2 min), we just launch it and let it self-log.
    //
    // TWO GUARDS, BOTH CROSS-PROCESS (2026-09-24 startup-loop fix; see StartupGate's doc
    // comment in ui/SelfImproveDashboard.cs): if start_all.ps1 is ALREADY running, launching a
    // second one only queues behind its lock and relaunches this cockpit again -- do nothing
    // and say so, rather than repeat the mechanism that caused the loop. Otherwise, the 120s
    // cooldown that used to live in this process's own fields (_startAllLaunched/
    // _startAllLastUnix, so a freshly relaunched cockpit always saw it as never-yet-fired) is
    // now the persisted "start_all" budget key, so a new process inherits the real cooldown.
    void RunStartAll()
    {
        if (WindowSelfTest.Active) return;   // a selftest never starts the stack
        if (IsStartAllRunning()) { NoteFromAnyThread(T("autofix_start_all_running")); return; }
        bool exhausted;
        if (!TryConsumeAutoFixBudget("start_all", 1, START_ALL_COOLDOWN_S, out exhausted)) return;
        try
        {
            string vbs = Path.Combine(RepoRoot(), "scripts", "start_all_hidden.vbs");
            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = "wscript.exe";
            psi.Arguments = "\"" + vbs + "\"";
            psi.WorkingDirectory = RepoRoot();
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            System.Diagnostics.Process.Start(psi);
            System.Diagnostics.Debug.WriteLine("[FleetCockpit] RunStartAll: launched " + vbs);
        }
        catch (Exception ex)
        {
            try
            {
                if (!Dispatcher.HasShutdownStarted)
                    Dispatcher.BeginInvoke(new Action(delegate { if (_fixNote != null) _fixNote.Text = T("hs_fix_err") + ": " + ex.Message; }));
            }
            catch (Exception) { }
        }
    }
}
