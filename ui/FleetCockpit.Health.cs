// FleetCockpit.Health.cs -- health strip and polling: dot state, PollHealthOnce, PublishHealthStrip, stale-server display.
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

    // ── P0 HEALTH STRIP ─────────────────────────────────────────────────────────────
    // Six infra dots (Server/Tunnel/Edge/Sign-in/Agent/Tool) in the header, always visible,
    // polled every ~15s on a background thread and marshaled to the UI via Dispatcher.
    // Colors: "green"|"red"|"yellow"|"gray". The motivating incident: the companion Edge
    // session died -> login wall -> agent fell back to default Copilot; the cockpit only
    // showed failing tasks. These dots make the infra cause glanceable and one-click fixable.
    // The 6th dot (Tool) is a DIFFERENT axis from the first 5: dots 0-4 all probe the FLEET
    // Edge (:9222) / server / tunnel, none of which tell you whether the INTERACTIVE bridge
    // chat (Edge profile copilot-bridge-edge, CDP :9223) can actually call an MCP tool. Tool
    // reads .fleet\tool_probe.json, written by the bridge's own idle self-probe (a separate
    // component -- bridge/copilot_bridge.py + tools/tool_probe.py -- this file only reads it).
    enum HealthState { Gray = 0, Green = 1, Yellow = 2, Red = 3, Checking = 4 }
    class DotState { public HealthState State = HealthState.Gray; public string Detail = ""; public DateTime Checked = DateTime.MinValue; }
    class PlannedServerTransition
    {
        public string State = "";
        public string Reason = "";
        public double Started = 0;
        public double Expires = 0;
        public int SupervisorPid = 0;
        public double SupervisorStarted = 0;
    }
    // Old supervisors wrote only `started`; preserve that format for one deployment generation.
    // New supervisors write their own policy-derived expiry. The hard cap is only corruption /
    // stale-file safety: it is intentionally NOT the normal restart budget.
    const double LEGACY_SERVER_TRANSITION_MAX_AGE_S = 60.0;
    const double SERVER_TRANSITION_HARD_MAX_AGE_S = 600.0;

    static DotState[] NewDotStates()
    {
        var a = new DotState[HEALTH_DOT_COUNT];
        for (int i = 0; i < a.Length; i++) a[i] = new DotState();
        return a;
    }
    readonly object _healthLock = new object();
    // EXACT HEALTH-POLL SELFTEST SEAMS. Null in every normal process. They exist so a temp
    // harness can execute PollHealthOnce() itself against an unreachable synthetic server and a
    // temp transition marker without stopping the production MCP server. Both uses are gated by
    // WindowSelfTest.Active below; normal cockpit behaviour cannot redirect either source.
    string _serverTransitionPathForSelfTest = null;
    Func<string, int, string> _serverHealthBodyForSelfTest = null;
    // The last /health body, captured by the Server dot's poll so dot 5 can read the FLEET
    // tool path from it without a second HTTP round trip. Empty until the first successful
    // poll, and empty must read as "no evidence" everywhere it is used.
    static string _lastHealthBody = "";

    //: WHEN that body was true, because a payload with no timestamp is indistinguishable from
    //: a current one. _lastHealthBody was kept for the lifetime of the process and never
    //: aged: once /health started failing, the last-known fleet_tool_ok went on softening the
    //: tool dot from red to amber for as long as the cockpit stayed open. The window is the
    //: same 30 seconds the poll runs on, doubled, so one missed sweep does not blank the dot
    //: and two do.
    static double _lastHealthBodyAt = 0;
    const double HEALTH_BODY_MAX_AGE_S = 60;
    // SIX. A 7th "frozen set" dot lived here 2026-09-19 to 2026-09-24: it named the
    // self-improvement judge-file check, sized to HEALTH_DOT_COUNT=7 like every other axis.
    // Owner feedback on 12b06fd (screenshot + "そもそも置く必要ある？"): the label did not fit
    // the strip even in plain language, and the strip is the wrong place for it -- the
    // self-improvement loop already refuses to run on drift, and the self-improvement
    // dashboard already shows the same fact with its own re-sign button. Removed rather than
    // shortened. SelfImproveDashboardWindow.FrozenGate / SelfImproveInUse existed only to
    // feed this dot and were removed with it; the dashboard's own plain-language labels
    // (auth_intact etc.) are unrelated and unchanged.
    const int HEALTH_DOT_COUNT = 6;
    Border[] _healthDot;           // the 6 colored dots (re-tinted by ApplyHealthToUi)
    FrameworkElement[] _healthSpin;  // rotating in-progress marks, shown instead of a stale color
    TextBlock[] _healthLbl;        // the 6 labels (re-textable on language toggle)
    Border[] _healthDotWrap;       // per-dot wrapper (tooltip host)
    Button _fixBtn;                // the 「直す」/Fix button (shown only when a dot is red/yellow)
    TextBlock _fixNote;            // inline progress/toast text in the strip
    Border _healthStrip;           // the whole fixed-width strip container
    Thread _healthThread;
    volatile bool _healthStop = false;
    volatile bool _fixRunning = false;   // guard: never run two fixes at once
    volatile int _fixTargetMask = 0;     // health axes currently being repaired; renders as Checking
    readonly AutoResetEvent _healthWake = new AutoResetEvent(false); // immediate post-action refresh
    volatile bool _bridgeReconnectRunning = false;   // guard: manual "Reconnect chat" button, never two at once
    string _agentMarkerId = "";    // T_.../P_... id extracted from the configured agent URL (.env)
    bool _startupHealCheckDone = false;        // set after the first PollHealthOnce's auto-heal decision runs once

    //: How long a server may be stale before it is worth a colour.
    //:
    //: The supervisor checks roughly every 30 s and requires the condition to persist across
    //: TWO consecutive checks before it cycles, and it only cycles while the fleet is idle. Ten
    //: minutes is comfortably past a healthy cycle and still short enough that a person who
    //: needs to know finds out in the same sitting. Below it, staleness is a fact in the detail
    //: line; above it, the machine has had its chance and failed.
    const double STALE_AMBER_AFTER_S = 600.0;

    static bool StaleLongEnoughToMatter(string srvBody)
    {
        string raw = HealthField(srvBody, "server_stale_for_s");
        // AN OLDER SERVER DOES NOT PUBLISH THIS, and absence must not read as zero -- that would
        // make every stale server green forever, which is the opposite failure and a worse one.
        // Unknown is treated as "worth a colour", the same way an unreadable state is elsewhere.
        if (string.IsNullOrEmpty(raw) || raw == "null") return true;
        double secs;
        if (!double.TryParse(raw, System.Globalization.NumberStyles.Float,
                             System.Globalization.CultureInfo.InvariantCulture, out secs))
            return true;
        return secs >= STALE_AMBER_AFTER_S;
    }

    // Build the health strip: 5 dots with labels + an INLINE Fix pill + an inline note, all on ONE
    // horizontal row. Redesign: left-aligned & vertically centered (it sits at the left edge of the
    // header where the title used to be). The Fix pill appears immediately to the RIGHT of the 5th
    // dot (エージェント) only when something is red/yellow; the note trails inline after it. No fixed
    // width and no second (stacked) row — the appearing/disappearing Fix affordance can only push the
    // note (both are the row's trailing items) and never shifts the right-side controls, which live in
    // a SEPARATE grid column (col 1).
    UIElement BuildHealthStrip()
    {
        _healthDot = new Border[HEALTH_DOT_COUNT];
        _healthSpin = new FrameworkElement[HEALTH_DOT_COUNT];
        _healthLbl = new TextBlock[HEALTH_DOT_COUNT];
        _healthDotWrap = new Border[HEALTH_DOT_COUNT];

        _healthStrip = new Border();
        _healthStrip.HorizontalAlignment = HorizontalAlignment.Left;   // flush to the header's left edge
        _healthStrip.VerticalAlignment = VerticalAlignment.Center;
        _healthStrip.Margin = new Thickness(0, 0, 12, 0);

        // Single horizontal row: [dot label] x6, then the inline Fix pill, then the inline note.
        var row = new StackPanel { Orientation = Orientation.Horizontal,
                                   HorizontalAlignment = HorizontalAlignment.Left,
                                   VerticalAlignment = VerticalAlignment.Center };
        string[] keys = { "hs_server", "hs_tunnel", "hs_edge", "hs_signin", "hs_agent", "hs_tool" };
        for (int i = 0; i < HEALTH_DOT_COUNT; i++)
        {
            var wrap = new Border();
            wrap.Margin = new Thickness(i == 0 ? 0 : 8, 0, 0, 0);
            wrap.Padding = new Thickness(0);
            wrap.VerticalAlignment = VerticalAlignment.Center;
            var dr = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            var mark = new Grid { Width = 10, Height = 10, VerticalAlignment = VerticalAlignment.Center,
                                  Margin = new Thickness(0, 0, 4, 0) };
            var dot = new Border { Width = 8, Height = 8, CornerRadius = new CornerRadius(8 / 2.0),
                                   HorizontalAlignment = HorizontalAlignment.Center,
                                   VerticalAlignment = VerticalAlignment.Center };
            var spin = BuildSpinner(10);
            spin.Visibility = Visibility.Collapsed;
            mark.Children.Add(dot);
            mark.Children.Add(spin);
            var lbl = new TextBlock { FontSize = 11, VerticalAlignment = VerticalAlignment.Center, Text = T(keys[i]) };
            dr.Children.Add(mark);
            dr.Children.Add(lbl);
            wrap.Child = dr;
            _healthDot[i] = dot;
            _healthSpin[i] = spin;
            _healthLbl[i] = lbl;
            _healthDotWrap[i] = wrap;
            row.Children.Add(wrap);
        }

        // INLINE Fix pill (hidden unless red/yellow): refined warning-outlined pill (transparent fill,
        // Warning outline + text) with a small Material Symbol + the label. Sits right after the 5th dot.
        _fixBtn = new Button();
        _fixBtn.Content = BuildFixPillContent(false);
        _fixBtn.FontSize = 11; _fixBtn.FontWeight = FontWeights.SemiBold;
        _fixBtn.Padding = new Thickness(8, 1, 8, 1);
        _fixBtn.Margin = new Thickness(12, 0, 0, 0);      // gap after エージェント dot
        _fixBtn.Cursor = Cursors.Hand;
        _fixBtn.BorderThickness = new Thickness(1);
        _fixBtn.Template = FlatButtonTemplate();          // rounded (CornerRadius 4) pill chrome
        _fixBtn.VerticalAlignment = VerticalAlignment.Center;
        _fixBtn.Visibility = Visibility.Collapsed;
        _fixBtn.ToolTip = T("hs_fix_hint");
        System.Windows.Automation.AutomationProperties.SetName(_fixBtn, T("hs_fix"));
        _fixBtn.Click += delegate { RunFix(); };
        row.Children.Add(_fixBtn);

        _fixNote = new TextBlock { FontSize = 11, VerticalAlignment = VerticalAlignment.Center,
                                   Margin = new Thickness(8, 0, 0, 0), Text = "",
                                   TextTrimming = TextTrimming.CharacterEllipsis, MaxWidth = 250 };
        row.Children.Add(_fixNote);

        _healthStrip.Child = row;
        PaintHealthChrome();
        ApplyHealthToUi();     // paint current cached states (Gray until first poll completes)
        return _healthStrip;
    }

    // The inline Fix pill's content: a small Material Symbol (settings/cog — the closest repair glyph
    // in the subset) at 14px + the localized "Fix" label, tinted with the Warning token to match the
    // pill's outline. Rebuilt on theme/lang flips via RebuildChrome (whole chrome is reconstructed).
    FrameworkElement BuildSpinner(double size)
    {
        var spin = (FrameworkElement)MakeIcon("refresh", size, Theme.Br(Theme.Warning(_dark)));
        spin.HorizontalAlignment = HorizontalAlignment.Center;
        spin.VerticalAlignment = VerticalAlignment.Center;
        spin.RenderTransformOrigin = new Point(0.5, 0.5);
        var rotate = new RotateTransform(0);
        spin.RenderTransform = rotate;
        var animation = new DoubleAnimation(0, 360, new Duration(TimeSpan.FromMilliseconds(850)));
        animation.RepeatBehavior = RepeatBehavior.Forever;
        rotate.BeginAnimation(RotateTransform.AngleProperty, animation);
        return spin;
    }

    UIElement BuildFixPillContent(bool busy)
    {
        var sp = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        UIElement ic = busy ? (UIElement)BuildSpinner(13) : MakeIcon("settings", 14, Theme.Br(Theme.Warning(_dark)));
        ((FrameworkElement)ic).Margin = new Thickness(0, 0, 4, 0);
        ((FrameworkElement)ic).VerticalAlignment = VerticalAlignment.Center;
        sp.Children.Add(ic);
        sp.Children.Add(new TextBlock { Text = T(busy ? "hs_fixing_button" : "hs_fix"), FontSize = 11, FontWeight = FontWeights.SemiBold,
                                        VerticalAlignment = VerticalAlignment.Center });
        return sp;
    }

    // Re-tint the strip chrome (labels, Fix button) for the current theme. Called from PaintChrome.
    void PaintHealthChrome()
    {
        if (_healthLbl != null)
            for (int i = 0; i < _healthLbl.Length; i++)
                if (_healthLbl[i] != null) _healthLbl[i].Foreground = Muted;
        if (_fixBtn != null)
        {
            // Warning-outline (needs-attention), NOT the reserved accent fill.
            _fixBtn.Background = Brushes.Transparent;
            _fixBtn.Foreground = Theme.Br(Theme.Warning(_dark));
            _fixBtn.BorderBrush = Theme.Br(Theme.Warning(_dark));
            _fixBtn.Content = BuildFixPillContent(_fixRunning);
        }
        // The spinner is drawn geometry now, so its colour is a Fill, not a Foreground. Cast
        // narrowly and skip anything that is not a Shape rather than assuming: this loop runs on
        // every theme flip and must not be able to throw.
        if (_healthSpin != null)
            for (int i = 0; i < _healthSpin.Length; i++)
            {
                var sh = _healthSpin[i] as System.Windows.Shapes.Shape;
                if (sh != null) sh.Fill = Theme.Br(Theme.Warning(_dark));
            }
        if (_fixNote != null) _fixNote.Foreground = Muted;
    }

    // Map a HealthState to its Theme dot color for the current mode.
    Brush HealthBrush(HealthState s)
    {
        if (s == HealthState.Green) return Theme.Br(Theme.Success(_dark));
        if (s == HealthState.Red) return Theme.Br(Theme.HealthRed(_dark));
        if (s == HealthState.Yellow) return Theme.Br(Theme.HealthAmber(_dark));
        if (s == HealthState.Checking) return Theme.Br(Theme.Warning(_dark));
        return Theme.Br(Theme.Muted(_dark));   // gray / unknown
    }

    // Apply the cached _health snapshot onto the dots + tooltips + Fix button visibility.
    // MUST run on the UI thread (called from BuildHealthStrip and from the Dispatcher marshal).
    void ApplyHealthToUi()
    {
        // BEFORE the early return: the reconnect control lives in the settings panel, which is
        // built and shown independently of the health dots. Putting this after the guard would
        // leave it stuck on whatever tint it was born with on any machine where `_healthDot` is
        // null -- which is the "it is always amber" symptom, reintroduced by the fix for it.
        RefreshReconnectChatTint();
        if (_healthDot == null) return;
        bool anyBad = false;
        DotState[] snap = new DotState[HEALTH_DOT_COUNT];
        lock (_healthLock)
            for (int i = 0; i < HEALTH_DOT_COUNT; i++)
                snap[i] = new DotState { State = _health[i].State, Detail = _health[i].Detail, Checked = _health[i].Checked };
        for (int i = 0; i < HEALTH_DOT_COUNT; i++)
        {
            bool checking = snap[i].State == HealthState.Checking || (_fixRunning && (_fixTargetMask & (1 << i)) != 0);
            if (_healthDot[i] != null)
            {
                _healthDot[i].Background = HealthBrush(snap[i].State);
                // Red = solid circle, yellow = smaller rounded square: distinct by shape as well as
                // by colour (the 10x10 mark grid fits both).
                bool isRed = snap[i].State == HealthState.Red;
                bool isAmber = snap[i].State == HealthState.Yellow;
                _healthDot[i].Width = _healthDot[i].Height = isRed ? 10 : 8;
                _healthDot[i].CornerRadius = new CornerRadius(isAmber ? 2 : (isRed ? 5 : 4));
                _healthDot[i].Visibility = checking ? Visibility.Collapsed : Visibility.Visible;
            }
            if (_healthSpin != null && _healthSpin[i] != null)
                _healthSpin[i].Visibility = checking ? Visibility.Visible : Visibility.Collapsed;
            if (snap[i].State == HealthState.Red || snap[i].State == HealthState.Yellow)
                anyBad = true;
            if (_healthDotWrap[i] != null)
            {
                string when = snap[i].Checked == DateTime.MinValue
                    ? T("hs_never")
                    : snap[i].Checked.ToLocalTime().ToString("HH:mm:ss");
                string detail = string.IsNullOrEmpty(snap[i].Detail) ? T("hs_checking") : snap[i].Detail;
                string level = snap[i].State == HealthState.Red ? T("hs_level_red") + " "
                             : snap[i].State == HealthState.Yellow ? T("hs_level_yellow") + " " : "";
                _healthDotWrap[i].ToolTip = level + T(_healthKeys[i]) + ": " + detail + "\n"
                    + T("hs_lastcheck") + when;
            }
        }
        if (_fixBtn != null)
        {
            // Never hide the button mid-fix (it is disabled while running so it can't re-enter).
            _fixBtn.Visibility = (anyBad || _fixRunning) ? Visibility.Visible : Visibility.Collapsed;
            _fixBtn.IsEnabled = !_fixRunning;
            _fixBtn.Content = BuildFixPillContent(_fixRunning);
        }
        // Queue starvation is a SEPARATE operational fact, not a seventh infra dot. If a
        // submitted item survives the worker-pickup budget after current/history reconciliation,
        // surface it here so the operator does not have to discover it by scrolling.
        SubmittedHealth queueHealth = SubmittedTasks.SummarizeHealth(ReadQueuedJobs());
        // NOTHING IS DRAINING THE QUEUE is worse than "unstarted", and is shown even when a dot is
        // red: relay/consumer_watch.py (hosted by the server) writes .fleet\consumer_alarm.json
        // while a job has waited past its limit with no supervisor, and removes it when one is back.
        string consumerAlarm = ReadConsumerAlarm();
        if (consumerAlarm.Length > 0 && !_fixRunning && _fixNote != null)
        {
            _fixNote.Text = consumerAlarm;
            _fixNote.Foreground = Theme.Br(Theme.Warning(_dark));
        }
        else if (!anyBad && !_fixRunning && _fixNote != null)
        {
            if (queueHealth.StaleCount > 0)
            {
                _fixNote.Text = string.Format(T("hs_queue_stale"), queueHealth.StaleCount,
                    SubmittedTasks.Age(queueHealth.OldestAgeS, _lang == 0));
                _fixNote.Foreground = Theme.Br(Theme.Warning(_dark));
            }
            else if (_fixNote.Text.Length > 0)
            {
                _fixNote.Text = "";
                _fixNote.Foreground = Muted;
            }
        }
    }
    // The alarm text in the cockpit's language, or "" when there is none (or it is stale: the
    // watcher rewrites it every ~15 s, so one not touched for 5 minutes is a dead watcher's).
    string ReadConsumerAlarm()
    {
        try
        {
            string path = Path.Combine(RepoRootForSettings(), ".fleet", "consumer_alarm.json");
            if (!File.Exists(path)) return "";
            if ((DateTime.UtcNow - File.GetLastWriteTimeUtc(path)).TotalSeconds > 300) return "";
            var m = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
            object t;
            if (m == null || !m.TryGetValue(_lang == 0 ? "text_ja" : "text_en", out t) || t == null) return "";
            return Convert.ToString(t);
        }
        catch (Exception) { return ""; }
    }
    static readonly string[] _healthKeys = { "hs_server", "hs_tunnel", "hs_edge", "hs_signin", "hs_agent", "hs_tool" };

    // Start (once) the background poll thread. Re-entrant-safe: only spawns if not already alive.
    // BuildChrome (and RebuildChrome) call this; a language flip rebuilds chrome but the thread keeps
    // running, so we don't restart it — we just refresh the UI from the still-updating cache.
    void StartHealthPoll()
    {
        if (WindowSelfTest.Active) return;   // probes the network and may start the stack
        _agentMarkerId = ExtractAgentMarker();
        if (_healthThread != null && _healthThread.IsAlive) { ApplyHealthToUi(); return; }
        _healthStop = false;
        _healthThread = new Thread(new ThreadStart(HealthLoop));
        _healthThread.IsBackground = true;   // dies with the app; never blocks shutdown
        _healthThread.Start();
    }

    // Background poll loop. ~15s cadence; each probe uses a short (3-4s) timeout so a dead
    // endpoint can't stall the sweep. NEVER touches WPF objects directly — it writes the cache
    // and marshals ApplyHealthToUi onto the Dispatcher.
    void HealthLoop()
    {
        while (!_healthStop)
        {
            try { PollHealthOnce(); } catch (Exception) { }

            // Startup auto-heal: once per app run, right after the FIRST sweep completes, check
            // whether the stack needs bringing up and do it ourselves -- this is what makes
            // launching FleetCockpit.exe directly (not via the desktop icon) self-healing too.
            // Guarded by _startupHealCheckDone (runs once), StartupGate (below -- start_all
            // already running, or this process was launched BY start_all), RunStartAll's own
            // persisted cooldown, and FleetRunIsLive(). See StartupGate's doc comment in
            // ui/SelfImproveDashboard.cs for the loop this whole gate exists to stop.
            if (!_startupHealCheckDone)
            {
                _startupHealCheckDone = true;
                HealthState srv0, tun0;
                lock (_healthLock) { srv0 = _health[0].State; tun0 = _health[1].State; }
                System.Diagnostics.Debug.WriteLine("[FleetCockpit] HealthLoop: startup auto-heal check server=" + srv0 + " tunnel=" + tun0);
                if (srv0 == HealthState.Red || tun0 == HealthState.Red)
                {
                    var gate = StartupGate.GateAutomaticLaunch(IsStartAllRunning(), LaunchedByStartAll(), true);
                    if (!gate.Allow)
                    {
                        if (!string.IsNullOrEmpty(gate.ReasonKey)) NoteFromAnyThread(T(gate.ReasonKey));
                    }
                    // THE SAME GUARD AS EVERY OTHER AUTOMATIC ACTION. This ran RunStartAll on the
                    // first sweep regardless of whether a fleet run was in flight, so a cockpit
                    // opened during a run could restart the stack underneath it.
                    else if (FleetRunIsLive()) { /* leave it to the person */ }
                    else
                    {
                        NoteFromAnyThread(T("hs_fix_stack"));
                        RunStartAll();
                    }
                }
            }

            try
            {
                if (!Dispatcher.HasShutdownStarted)
                    Dispatcher.BeginInvoke(new Action(delegate { try { ApplyHealthToUi(); } catch (Exception) { } }));
            }
            catch (Exception) { }
            // Normally poll every 15s, but wake immediately after a repair action changes state.
            // This removes the old dead interval where the user clicked Fix, the command had
            // already completed, yet the strip kept showing the pre-fix red snapshot.
            if (!_healthStop) _healthWake.WaitOne(15000);
        }
    }

    PlannedServerTransition ReadPlannedServerTransition()
    {
        try
        {
            string path = (WindowSelfTest.Active && !string.IsNullOrEmpty(_serverTransitionPathForSelfTest))
                ? _serverTransitionPathForSelfTest
                : Path.Combine(RepoRootForSettings(), ".fleet", "server_transition.json");
            if (!File.Exists(path)) return null;
            var raw = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
            if (raw == null) return null;
            object stateObj, reasonObj, startedObj, expiresObj, supervisorPidObj, supervisorStartedObj;
            if (!raw.TryGetValue("state", out stateObj) || stateObj == null) return null;
            string state = Convert.ToString(stateObj).Trim().ToLowerInvariant();
            if (state != "planned_restart") return null;
            if (!raw.TryGetValue("started", out startedObj) || startedObj == null) return null;
            double started = Convert.ToDouble(startedObj, System.Globalization.CultureInfo.InvariantCulture);
            double nowUnix = NowUnix();
            double age = nowUnix - started;
            if (age < -5.0 || age > SERVER_TRANSITION_HARD_MAX_AGE_S) return null;

            double expires = started + LEGACY_SERVER_TRANSITION_MAX_AGE_S;
            if (raw.TryGetValue("expires", out expiresObj) && expiresObj != null)
                expires = Convert.ToDouble(expiresObj, System.Globalization.CultureInfo.InvariantCulture);
            double declaredWindow = expires - started;
            if (declaredWindow <= 0.0 || declaredWindow > SERVER_TRANSITION_HARD_MAX_AGE_S) return null;
            double effectiveExpiry = Math.Min(expires, started + SERVER_TRANSITION_HARD_MAX_AGE_S);
            if (nowUnix > effectiveExpiry) return null;

            // OWNERSHIP IS PART OF FRESHNESS. A PID alone is unsafe on Windows because it can be
            // reused after the supervisor dies. New markers therefore name both PID and process
            // birth. If either is missing/unreadable/mismatched, fail closed: an unverifiable
            // marker is never permission to soften a real server outage from red to yellow.
            if (!raw.TryGetValue("supervisor_pid", out supervisorPidObj) || supervisorPidObj == null) return null;
            if (!raw.TryGetValue("supervisor_started", out supervisorStartedObj) || supervisorStartedObj == null) return null;
            int supervisorPid = Convert.ToInt32(supervisorPidObj, System.Globalization.CultureInfo.InvariantCulture);
            double supervisorStarted = Convert.ToDouble(supervisorStartedObj, System.Globalization.CultureInfo.InvariantCulture);
            if (supervisorPid <= 0 || supervisorStarted <= 0.0) return null;
            try
            {
                var supervisorProcess = System.Diagnostics.Process.GetProcessById(supervisorPid);
                if (supervisorProcess.HasExited) return null;
                double processStarted = new DateTimeOffset(supervisorProcess.StartTime.ToUniversalTime()).ToUnixTimeSeconds();
                if (Math.Abs(processStarted - supervisorStarted) > 2.0) return null;
            }
            catch (Exception)
            {
                return null;
            }

            string reason = "planned restart";
            if (raw.TryGetValue("reason", out reasonObj) && reasonObj != null)
                reason = Convert.ToString(reasonObj).Trim();
            if (string.IsNullOrEmpty(reason)) reason = "planned restart";
            return new PlannedServerTransition { State = state, Reason = reason, Started = started, Expires = expires, SupervisorPid = supervisorPid, SupervisorStarted = supervisorStarted };
        }
        catch (Exception)
        {
            // An unreadable transition is not permission to soften a red health signal.
            return null;
        }
    }

    // Execute the REAL server segment of PollHealthOnce in a --selftest process. This is not a
    // second implementation: it only supplies the two external inputs (HTTP body + marker path),
    // calls PollHealthOnce below, and returns the dot state it actually wrote. Production callers
    // cannot use it because WindowSelfTest.Active is false.
    internal string SelfTestServerHealthPoll(string transitionPath, bool reachable)
    {
        if (!WindowSelfTest.Active)
            throw new InvalidOperationException("server health selftest seam is available only under --selftest");
        string oldPath = _serverTransitionPathForSelfTest;
        Func<string, int, string> oldBody = _serverHealthBodyForSelfTest;
        try
        {
            _serverTransitionPathForSelfTest = transitionPath;
            _serverHealthBodyForSelfTest = delegate(string url, int timeoutMs)
            {
                if (!reachable) return null;
                return "{\"status\":\"ok\",\"auth_fail_10m\":0,\"server_code\":\"current\"}";
            };
            PollHealthOnce();
            lock (_healthLock)
                return _health[0].State.ToString() + "|" + (_health[0].Detail ?? "");
        }
        finally
        {
            _serverTransitionPathForSelfTest = oldPath;
            _serverHealthBodyForSelfTest = oldBody;
        }
    }

    // One full infra sweep. Writes results into _health under _healthLock.
    void PollHealthOnce()
    {
        DateTime now = DateTime.UtcNow;
        PlannedServerTransition plannedRestart = ReadPlannedServerTransition();

        // 0) Server: GET http://127.0.0.1:8000/health, and READ WHAT IT SAYS.
        //
        // A 200 means the event loop answered. It does not mean the server is doing its job:
        // the handler is deliberately non-blocking (main.py:303-309), so it returns 200 while
        // authentication is failing and while every tool call is refused. Green on the status
        // code alone is the single most trusted dot reporting the least.
        //
        // So: unreachable stays Red, and a reachable server that is REPORTING A PROBLEM about
        // itself goes Amber rather than Green. Amber, not Red, because the server process is
        // genuinely up -- the distinction matters for what a person does next.
        string srvBody = (WindowSelfTest.Active && _serverHealthBodyForSelfTest != null)
            ? _serverHealthBodyForSelfTest("http://127.0.0.1:8000/health", 3500)
            : HttpBody("http://127.0.0.1:8000/health", 3500);
        bool srvOk = srvBody != null;
        if (srvOk) { _lastHealthBody = srvBody; _lastHealthBodyAt = NowUnix(); }
        string authFails = HealthField(srvBody, "auth_fail_10m");
        // A BURST, NOT A STRAY. This first read "any non-zero count is amber", which is the
        // same mistake as judging the fleet tool path on a single failure -- caught there by
        // running it, not caught here. Measured 2026-09-16: one 401 from 127.0.0.1, generated
        // by this repository is own test suite touching the live server, ambered the most
        // trusted dot for ten minutes.
        //
        // What this dot exists to catch is a key desync (Copilot Studio holding a stale
        // MCP_API_KEY): every gated call then fails, so the count is dozens within the same
        // ten-minute window, not one. Three separates that from a stray probe or a single
        // retry, and is deliberately far below what a real desync produces.
        int authFailN = 0; int.TryParse(authFails, out authFailN);
        bool authStorm = authFailN >= 3;
        // STALE CODE IS NOT A HEALTHY SERVER, and 200 cannot tell you. A running process keeps
        // executing what it imported at startup; a pull lands new code and every dot stays
        // green while the checkout and the live process disagree. doctor.ps1:690 has checked
        // this for a while; this dot never did. On 2026-09-16 the process serving all morning
        // had started the previous evening, before every fix of that night, and was reported
        // healthy. server_code is computed by the server itself via the pure, pytest-covered
        // scripts/stale_server_check.classify_staleness.
        string codeState = HealthField(srvBody, "server_code");
        if (!srvOk)
        {
            if (plannedRestart != null)
                SetDot(0, HealthState.Yellow,
                       (_lang == 0 ? "計画されたサーバ再起動中: " : "Planned server restart in progress: ")
                       + plannedRestart.Reason, now);
            else
                SetDot(0, HealthState.Red, T("hs_srv_detail_bad"), now);
        }
        else if (authStorm)
            SetDot(0, HealthState.Red,
                   T("hs_srv_detail_auth") + " (" + authFails + ")", now);
        else if (codeState == "stale" && StaleLongEnoughToMatter(srvBody))
            SetDot(0, HealthState.Yellow,
                   T("hs_srv_detail_stale") + " (" + HealthField(srvBody, "server_head") + ")", now);
        else if (codeState == "stale")
            // GREEN, AND IT SAYS WHY. A commit that touches a watched package makes the running
            // server genuinely stale, so on a machine where an agent improves the code all day
            // this was amber almost all of the time -- and a colour that is on in the normal
            // working state distinguishes nothing. The operator put it plainly on 2026-09-18:
            // "if that is the condition, the colour is only a false report."
            //
            // The FACT is still true and still shown, in the detail line. What was wrong was
            // treating it as something a person must act on: the supervisor cycles the server
            // itself once the fleet is idle. So the colour now marks the case a person can do
            // something about -- stale for longer than the machine should have needed, meaning
            // the cycle could not run or did not work.
            SetDot(0, HealthState.Green, T("hs_srv_detail_stale_recent"), now);
        else
            SetDot(0, HealthState.Green, T("hs_srv_detail_ok"), now);

        if (WindowSelfTest.Active && _serverHealthBodyForSelfTest != null) return;

        // 1) Tunnel: read MCP_TUNNEL_URL from ..\.env; GET <url>/health == 200. Gray if none.
        //
        // BUT FIRST: has the supervisor (scripts/supervisor.ps1, commit 57ad0d1) already worked
        // out that ANOTHER PC is the one actually serving THIS PC's tunnel right now? A probe
        // through the tunnel cannot tell "nobody is listening" from "someone else answered for
        // me" -- both read as the same failed (or, for "shared", even a SUCCEEDING) GET -- so
        // when .fleet\tunnel_host.json says foreign/shared, and the supervisor that wrote it is
        // still alive, that verdict overrides the generic probe below: red, with the file's own
        // plain message and action, not "server not reachable through the tunnel" -- which may
        // even be false (a foreign host can answer fine) and which tells the operator to look in
        // the wrong place either way. "ours"/"none" (or no file / dead supervisor) add nothing
        // the probe does not already establish, so they fall through to it unchanged.
        TunnelHostState tunHost = ReadTunnelHostState();
        if (tunHost != null && (tunHost.State == "foreign" || tunHost.State == "shared"))
        {
            string detail = tunHost.Message;
            if (!string.IsNullOrEmpty(tunHost.Action)) detail = detail + "  " + tunHost.Action;
            if (string.IsNullOrEmpty(detail)) detail = T("hs_tun_detail_bad");  // file present, no text -- never show a blank dot
            SetDot(1, HealthState.Red, detail, now);
        }
        else
        {
        string tunnel = EnvValue("MCP_TUNNEL_URL");
        if (string.IsNullOrEmpty(tunnel))
            // AMBER, NOT GRAY. Gray reads as "no evidence expected" and shows no Fix button,
            // but an unset MCP_TUNNEL_URL is not an absence of evidence -- it means the agent
            // cannot reach this server at all. Being told nothing is wrong while the whole
            // remote path is unusable is the failure this file keeps finding.
            SetDot(1, HealthState.Yellow, T("hs_tun_detail_none"), now);
        else
        {
            // MCP_TUNNEL_URL points at the /mcp path (e.g. https://host.devtunnels.ms/mcp);
            // /health is a SIBLING route at the tunnel origin, not nested under /mcp -- so
            // naively appending "/health" produced .../mcp/health, a 404 that always red'd
            // this dot even when the tunnel was serving correctly. Use the origin instead.
            string origin = tunnel;
            try { Uri u = new Uri(tunnel); origin = u.GetLeftPart(UriPartial.Authority); } catch (Exception) { }
            string turl = origin + "/health";
            // 6s, not the local 4s budget: this is a remote round-trip (devtunnels region)
            // that on a corporate machine also traverses the system proxy -- 4s false-reds it.
            // REACHING *A* SERVER IS NOT REACHING *THIS* SERVER. A 200 through the tunnel
            // proves something answered; it does not prove the tunnel forwards to the process
            // this machine is running. A tunnel left pointing at a previous host, or at a
            // second instance, answers 200 all day while the agent talks to the wrong server
            // and nothing on this strip disagrees.
            //
            // /health now names the process (server_pid). Comparing it to the pid loopback
            // just reported turns "something answered" into "the thing I meant answered".
            // Both sides empty means an older server build on one end -- no evidence, so it
            // does not colour anything.
            string tunBody = HttpBody(turl, 6000);
            bool tunOk = tunBody != null;
            string tunPid = HealthField(tunBody, "server_pid");
            string locPid = HealthField(srvBody, "server_pid");
            bool pidsDisagree = tunPid.Length > 0 && locPid.Length > 0 && tunPid != locPid;
            if (!tunOk)
            {
                // If the local server is deliberately between processes, the tunnel cannot reach
                // it either.  That is the same planned transition, not a second independent fault.
                if (plannedRestart != null && !srvOk)
                    SetDot(1, HealthState.Yellow,
                           (_lang == 0 ? "計画されたサーバ再起動に伴いトンネル待機中"
                                       : "Tunnel waiting for the planned server restart"), now);
                else
                    SetDot(1, HealthState.Red, T("hs_tun_detail_bad"), now);
            }
            else if (pidsDisagree)
                SetDot(1, HealthState.Yellow,
                       T("hs_tun_detail_other") + " (" + tunPid + " != " + locPid + ")", now);
            else
                SetDot(1, HealthState.Green, T("hs_tun_detail_ok"), now);
        }
        }

        // 2) Edge: CDP answers, AND a tab is actually on the agent.
        //
        // This tested /json/version alone, which only says a browser process is listening. On
        // 2026-08-31 the fleet's Edge sat on about:blank for hours -- every worker fell back to
        // the assistant with no tools and wrote patches from memory -- and this dot was GREEN
        // throughout, because a browser was indeed listening. A dot that is green during the
        // incident it exists to surface is not a signal.
        //
        // /json/list over plain HTTP, not Playwright: connect_over_cdp on a 15-second poll is
        // heavy and was measured interfering with the bridge's own page. The page's URL is
        // enough to tell "on the agent" from "blank".
        string edgeVersion = HttpGetBody("http://127.0.0.1:9222/json/version", 3500);
        bool edgeOk = edgeVersion != null;
        if (!edgeOk)
        {
            SetDot(2, HealthState.Red, T("hs_edge_detail_bad"), now);
        }
        else
        {
            string tabs = HttpGetBody("http://127.0.0.1:9222/json/list", 3500);
            // THE DOMAIN IS NOT THE AGENT. This asked whether "m365.cloud.microsoft" appeared
        // anywhere in the /json/list body, which is satisfied by a tab on the DEFAULT
        // Copilot -- no connectors, no tenant grounding -- the failure this file elsewhere
        // renders as a warning badge. It is also satisfied by another agent's tab, and by the
        // string turning up in a title, a description or a favicon URL of any target, since
        // nothing filters to "type":"page". The tunnel dot carries a comment about exactly
        // this class: a 200 proves something answered, not that it was the right something.
        //
        // _agentMarkerId is the T_/P_ id parsed from MCP_FLEET_AGENT_URL and is already used
        // for this judgement elsewhere in this file. When it is unset there is nothing to
        // check against, and the domain is then the most that can honestly be claimed.
        bool onDomain = tabs != null
                     && tabs.IndexOf("m365.cloud.microsoft", StringComparison.OrdinalIgnoreCase) >= 0;
        bool onAgent = onDomain
                    && (string.IsNullOrEmpty(_agentMarkerId)
                        || tabs.IndexOf(_agentMarkerId, StringComparison.OrdinalIgnoreCase) >= 0);
            if (onAgent)
                SetDot(2, HealthState.Green, T("hs_edge_detail_ok"), now);
            else if (!FleetRunIsLive() || !RunDrivesTabs())
                // A MISSING TAB IS ONLY A FAULT WHERE TABS ARE THE TRANSPORT.
                //
                // This asked for a tab unconditionally, and both ways round it was wrong.
                // Measured 2026-09-04: amber for the whole of a healthy socket-route run, whose
                // workers correctly hold no page -- then GREEN the moment that run ended STUCK,
                // because the failure left a Copilot page behind. The dot was inverted with
                // respect to the thing it is read for, which is worse than absent: a signal that
                // is wrong during the incident it exists to surface teaches people to ignore it.
                //
                // The original case is kept exactly. When a run IS driving tabs and none of them
                // is on the agent, that is still the 2026-08-31 failure -- every worker falling
                // back to the assistant with no tools -- and it still goes amber below.
                //
                // Whether workers can actually reach the agent is the AGENT dot's question, and
                // that one was already moved off the tab list when the socket route landed. This
                // dot answers for the browser.
                //
                // ITS OWN WORDING, THOUGH. Both green branches are correct and they mean
                // different things -- a tab sitting on the agent, versus a run that needs no
                // tab at all -- and they shared one detail string, so the strip could not tell
                // a reader which. The colour is right in both cases; only the sentence was
                // missing.
                SetDot(2, HealthState.Green,
                       T(FleetRunIsLive() ? "hs_edge_detail_notabs" : "hs_edge_detail_norun"),
                       now);
            else
                // TWO DIFFERENT FAULTS, AND THE WORSE ONE HAD NO MESSAGE. No m365 tab at all
                // is visible: the run has nowhere to go and the repair navigates. An m365 tab
                // that is NOT the configured agent is the silent one -- the default Copilot
                // answers fluently with no connectors and no tenant grounding, which is the
                // 2026-08-31 failure where two benchmark runs produced patches written from
                // memory. hs_agent_warn was written for exactly this and had never been
                // reachable, because until the marker check above the dot could not tell a
                // wrong tab from the right one.
                //
                // Amber for both: the browser is up and one navigation away from usable,
                // which is what the automatic repair is for.
                SetDot(2, HealthState.Yellow,
                       T(onDomain ? "hs_agent_warn" : "hs_edge_detail_blank"), now);
        }

        // 5) Tool: independent of the fleet Edge (:9222) probed above -- this reads the BRIDGE's
        //    own idle self-probe result (.fleet/tool_probe.json). Runs unconditionally (not gated
        //    on edgeOk) because it reflects a completely separate Edge profile/CDP port (:9223).
        PollToolProbeOnce(now);

        // 3+4) Sign-in and Agent USED to derive from the tab list at :9222/json, because
        //      when this was written every worker drove a tab. Work runs over a websocket
        //      now and a page is opened only to read a token -- roughly once per token
        //      lifetime, for four seconds, and not at all in between. So the tab list is
        //      empty during normal operation, and both dots were reading the emptiness:
        //
        //        sign-in  needsSignin = onLoginWall && !hasUsableM365Chat. With no tabs at
        //                 all that is false, so the dot was GREEN -- green because nothing
        //                 was there, not because sign-in worked, and equally green with an
        //                 EXPIRED sign-in. Health reported from an absence fails open.
        //
        //        agent    RunIsLive() && !hasUsableM365Chat -> RED. During a socket run
        //                 there is never a chat tab, so this was a guaranteed false red for
        //                 the whole run.
        //
        //      THE RULE THAT REPLACES THEM, applied to both: grey means there is no
        //      evidence and none is expected; green means fresh POSITIVE evidence; evidence
        //      that is expected and missing is neither -- amber, then red.
        //
        //      The evidence is .fleet/capture_status.json, written by relay/capture_floor.py
        //      at the one seam every capture passes through. It carries the token's expiry
        //      and audience, the agent the template names, and whether the capture worked.
        //      Never the token: an expiry and an audience grant nothing on their own.
        UpdateCaptureDots(now);

        MaybeAutoFix();
        PublishHealthStrip();
    }

    //: WRITE THE STRIP DOWN, because until 2026-09-17 it existed only on the screen.
    //:
    //: Asked why the server dot was lit, the command-line tool could say the server was
    //: reachable and that its code was stale -- it says both -- but not what the DOT was
    //: showing, which is what a person actually looks at. The answer had to be read off a
    //: tooltip by hand. That is the inversion of "the command line first, then the GUI".
    //:
    //: The file's own mtime is half the value: a strip that stopped being swept is a
    //: different failure from a strip that is all green, and neither was visible before.
    //: Never raises -- a panel that fell over while publishing its health would be reporting
    //: the opposite of what happened.
    void PublishHealthStrip()
    {
        try
        {
            string dir = Path.Combine(RepoRootForSettings(), ".fleet");
            Directory.CreateDirectory(dir);
            var sb = new System.Text.StringBuilder();
            var inv = System.Globalization.CultureInfo.InvariantCulture;
            sb.Append("{\"ts\":").Append(NowUnix().ToString("F3", inv));
            sb.Append(",\"dots\":[");
            lock (_healthLock)
            {
                for (int i = 0; i < HEALTH_DOT_COUNT; i++)
                {
                    if (i > 0) sb.Append(',');
                    double age = _health[i].Checked == DateTime.MinValue
                        ? -1.0 : (DateTime.UtcNow - _health[i].Checked).TotalSeconds;
                    sb.Append("{\"key\":\"").Append(JsonEscape(_healthKeys[i]))
                      .Append("\",\"state\":\"")
                      .Append(_health[i].State.ToString().ToLowerInvariant())
                      .Append("\",\"detail\":\"").Append(JsonEscape(_health[i].Detail ?? ""))
                      .Append("\",\"checked_age_s\":").Append(age.ToString("F1", inv))
                      .Append('}');
                }
            }
            sb.Append(']');
            // AND THE QUEUE THIS PANEL IS SHOWING.
            //
            // The rule in this project is that work which cannot be confirmed in the GUI does
            // not count as working, and a job submitted to the fleet was invisible here until a
            // worker existed. The display is fixed; this is how the claim is CHECKABLE without
            // asking someone to describe their screen -- the same reason the dots above are
            // published, added the day before for the same complaint.
            //
            // It is the panel reporting what IT rendered, not a second opinion computed from
            // the same files. That distinction is the whole value: a reader comparing this
            // against .fleet/tasks/pending can see the display and the truth disagree.
            var qj = ReadQueuedJobs();
            SubmittedHealth qh = SubmittedTasks.SummarizeHealth(qj);
            // WHERE IT LOOKED. A panel that reports "nothing queued" without saying where
            // it looked cannot be checked against the queue on disk -- which is the one
            // comparison this publication exists to make possible.
            sb.Append(",\"queue_dir\":\"").Append(JsonEscape(TasksDir())).Append('"');
            sb.Append(",\"queued_count\":").Append(qj.Count.ToString(inv));
            sb.Append(",\"queued_stale_count\":").Append(qh.StaleCount.ToString(inv));
            sb.Append(",\"queued_taken_stale_count\":").Append(qh.TakenStaleCount.ToString(inv));
            sb.Append(",\"queued_oldest_age_s\":").Append(qh.OldestAgeS.ToString("F1", inv));
            sb.Append(",\"consumer_alarm\":\"").Append(JsonEscape(ReadConsumerAlarm())).Append('"');
            sb.Append(",\"queued\":[");
            for (int qi = 0; qi < qj.Count && qi < 20; qi++)
            {
                if (qi > 0) sb.Append(',');
                // `where` is the entry's source in the merged "submitted" group: local (this
                // window, no file yet), command (commands.d), pending, for_fleet, or taken (its
                // file was consumed and no worker exists yet). `unconfirmed` is the stale mark.
                sb.Append("{\"id\":\"").Append(JsonEscape(qj[qi].Id ?? ""))
                  .Append("\",\"where\":\"").Append(JsonEscape(qj[qi].Source ?? ""))
                  .Append("\",\"unconfirmed\":").Append(qj[qi].Unconfirmed ? "true" : "false")
                  .Append(",\"label\":\"").Append(JsonEscape(SubmittedTasks.Label(qj[qi], _lang == 0)))
                  .Append("\",\"age_s\":").Append(qj[qi].AgeS.ToString("F1", inv))
                  .Append(",\"goal_head\":\"").Append(JsonEscape(OneLine(qj[qi].Goal, 90)))
                  .Append("\"}");
            }
            sb.Append("]}");
            string path = Path.Combine(dir, "health_strip.json");
            string tmp = path + ".tmp";
            File.WriteAllText(tmp, sb.ToString(), new System.Text.UTF8Encoding(false));
            if (File.Exists(path)) File.Delete(path);
            File.Move(tmp, path);
        }
        catch (Exception) { }
    }

    static string JsonEscape(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new System.Text.StringBuilder(s.Length + 8);
        foreach (char c in s)
        {
            if (c == '"' || c == '\\') { sb.Append('\\').Append(c); }
            else if (c == '\n') sb.Append("\\n");
            else if (c == '\r') sb.Append("\\r");
            else if (c == '\t') sb.Append("\\t");
            else if (c < ' ') sb.Append("\\u").Append(((int)c).ToString("x4"));
            else sb.Append(c);
        }
        return sb.ToString();
    }

    // Is a fleet run in flight.
    //
    // TWO SOURCES, BECAUSE status.json ALONE HAS A STARTUP GAP. The runner writes it
    // atomically, so once created it never vanishes -- absent therefore means "no run has ever
    // started here", and that is safe to repair in. But between a run launching and its first
    // snapshot, status.json still holds the PREVIOUS run's final state, with running=false. An
    // automatic Edge restart in that window would take the browser away from a run that had
    // just started, and status.json would have said it was fine to.
    //
    // fleet_run_active.json is written by the runner at launch and carries its pid. A live pid
    // there means a run exists whatever the snapshot says. Anything unreadable counts as live:
    // an unknown state must never authorise restarting the browser.
    // True when the run marker names a pid that no longer exists -- i.e. there is definitely
    // no run, independent of whatever status.json says or fails to say.
    bool MarkerPidIsDead()
    {
        try
        {
            string marker = Path.Combine(Path.GetDirectoryName(ResolvePath(null)),
                                         "fleet_run_active.json");
            if (!File.Exists(marker)) return true;
            var m = _js.DeserializeObject(File.ReadAllText(marker, Encoding.UTF8))
                    as Dictionary<string, object>;
            object pidObj;
            if (m == null || !m.TryGetValue("pid", out pidObj) || pidObj == null) return true;
            try { System.Diagnostics.Process.GetProcessById(Convert.ToInt32(pidObj)); return false; }
            catch (ArgumentException) { return true; }
        }
        catch (Exception) { return false; }   // cannot tell -> assume a run exists
    }

    // Is the runner PROCESS named by fleet_run_active.json still the same live process? This is
    // the liveness test for anything destructive. status.json's `updated` age is NOT: a runner
    // that is wedged or slow (long eval, overloaded machine) stops refreshing it while still
    // owning every unfinished worker. Mirrors relay/fleet_runner.marker_owner_alive: pid must
    // exist AND, when the marker carries pid_birth (process create time, ms since epoch), the
    // process start time must match, so a recycled pid is not mistaken for the runner.
    // Missing marker or a pid that is gone -> false. Anything unreadable/unprovable -> true
    // (an unknown state must never authorise discarding work).
    bool RunnerProcessAlive()
    {
        try
        {
            string marker = Path.Combine(Path.GetDirectoryName(ResolvePath(null)),
                                         "fleet_run_active.json");
            if (!File.Exists(marker)) return false;
            var m = _js.DeserializeObject(File.ReadAllText(marker, Encoding.UTF8))
                    as Dictionary<string, object>;
            object pidObj;
            if (m == null || !m.TryGetValue("pid", out pidObj) || pidObj == null) return true;
            int pid = Convert.ToInt32(pidObj);
            if (pid <= 0) return false;
            System.Diagnostics.Process p;
            try { p = System.Diagnostics.Process.GetProcessById(pid); }
            catch (ArgumentException) { return false; }
            using (p)
            {
                try { if (p.HasExited) return false; } catch (Exception) { return true; }
                long recorded = 0;
                object birthObj;
                if (m.TryGetValue("pid_birth", out birthObj) && birthObj != null)
                {
                    try { recorded = Convert.ToInt64(birthObj); } catch (Exception) { recorded = 0; }
                }
                if (recorded <= 0) return true;                       // old marker: pid is all we have
                long actual;
                try
                {
                    actual = (long)(p.StartTime.ToUniversalTime()
                                    - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalMilliseconds;
                }
                catch (Exception) { return true; }                    // cannot read start time -> cannot disprove
                return Math.Abs(actual - recorded) <= 5000;           // psutil vs .NET clocks differ by < 1 s
            }
        }
        catch (Exception) { return true; }
    }

    bool FleetRunIsLive()
    {
        try
        {
            string marker = Path.Combine(Path.GetDirectoryName(ResolvePath(null)),
                                         "fleet_run_active.json");
            if (File.Exists(marker))
            {
                var m = _js.DeserializeObject(File.ReadAllText(marker, Encoding.UTF8))
                        as Dictionary<string, object>;
                if (m == null) return true;
                object pidObj;
                if (m.TryGetValue("pid", out pidObj) && pidObj != null)
                {
                    int pid = Convert.ToInt32(pidObj);
                    try { System.Diagnostics.Process.GetProcessById(pid); return true; }
                    catch (ArgumentException) { /* the pid is gone: fall through to status */ }
                    catch (Exception) { return true; }
                }
            }
        }
        catch (Exception) { return true; }

        try
        {
            string p = ResolvePath(null);
            if (!File.Exists(p)) return false;      // never started here
            var root = _js.DeserializeObject(File.ReadAllText(p, Encoding.UTF8))
                       as Dictionary<string, object>;
            if (root == null) return !MarkerPidIsDead();
            if (!root.ContainsKey("running")) return !MarkerPidIsDead();
            return Convert.ToBoolean(root["running"]);
        }
        // UNREADABLE IS NOT "LIVE FOR EVER". Failing safe here is right, but a corrupt file
        // that nobody notices would otherwise disable automatic repair permanently -- the
        // guard would outlive the run it was guarding. When the runner's own pid marker says
        // no process exists, there is no run to protect, whatever the snapshot says.
        catch (Exception) { return !MarkerPidIsDead(); }
    }

    // Parsed .fleet\tunnel_host.json, written by scripts/supervisor.ps1 (commit 57ad0d1): the
    // supervisor's own verdict on who is CURRENTLY serving this PC's tunnel. State is one of
    // "ours" (this machine's supervisor owns it -- nothing to add), "none" (nobody -- the health
    // probe below already says so), "foreign" (another PC's supervisor answered for this one) or
    // "shared" (both this PC and another appear to be serving it). `message`/`action` are the
    // supervisor's own plain-language explanation and suggested next step -- written once, at
    // the place that actually knows which PC is which, rather than re-guessed here from a failed
    // GET that looks identical for "nobody is listening".
    class TunnelHostState
    {
        public string State = "";
        public string Message = "";
        public string Action = "";
    }

    // Read tunnel_host.json, but ONLY while the supervisor_pid it names is still alive. A file
    // is a snapshot, not a subscription: a supervisor that has since exited (this PC's own
    // supervisor started, superseding the foreign one; the operator killed it; a reboot) leaves
    // its last verdict sitting on disk, and treating that stale verdict as still true would keep
    // reporting a hijack that ended when the process that observed it did. Returns null on a
    // missing file, a dead supervisor_pid, or any parse failure -- all three mean "this file has
    // nothing to add right now", which the caller treats the same as state=="ours"/"none".
    TunnelHostState ReadTunnelHostState()
    {
        try
        {
            string path = Path.Combine(Path.GetDirectoryName(ResolvePath(null)), "tunnel_host.json");
            if (!File.Exists(path)) return null;
            var d = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8))
                    as Dictionary<string, object>;
            if (d == null) return null;
            object pidObj;
            if (!d.TryGetValue("supervisor_pid", out pidObj) || pidObj == null) return null;
            int pid;
            try { pid = Convert.ToInt32(pidObj); } catch (Exception) { return null; }
            try { System.Diagnostics.Process.GetProcessById(pid); }
            catch (ArgumentException) { return null; }   // the supervisor that wrote this is gone
            var t = new TunnelHostState();
            t.State = S(d, "state");
            t.Message = S(d, "message");
            t.Action = S(d, "action");
            return t;
        }
        catch (Exception) { return null; }
    }

    // Whether the live run drives TABS at all. Under the socket route it does not: workers hold
    // no page, and one is opened only to read a token -- about four seconds per token lifetime,
    // which the runner logs as 46 minutes. So "no tab is on the agent" is the normal state for
    // roughly 99.8% of a healthy run, and asking for one is asking about a transport this run
    // is not using. Returns true when the answer is not knowable, which keeps the original tab
    // check in force rather than assuming the socket route.
    bool RunDrivesTabs()
    {
        try
        {
            string p = ResolvePath(null);
            if (!File.Exists(p)) return false;
            var root = _js.DeserializeObject(File.ReadAllText(p, Encoding.UTF8))
                       as Dictionary<string, object>;
            if (root == null || !root.ContainsKey("open_tabs")) return true;   // unknown -> ask
            return I(root, "open_tabs") > 0;
        }
        catch (Exception) { return true; }
    }
}
