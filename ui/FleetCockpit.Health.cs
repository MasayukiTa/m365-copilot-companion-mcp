// FleetCockpit.Health.cs -- health strip, signal evaluation, and auto-repair.
// Physical split only: behavior remains on the same CockpitWindow partial class.
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
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;
using System.Globalization;
using System.Web.Script.Serialization;

partial class CockpitWindow
{

    // ══════════════════════════════════════════════════════════════════════════════════
    //  P0 HEALTH STRIP  —  infra state, glanceable + one-click fixable
    // ══════════════════════════════════════════════════════════════════════════════════
    // Prefer the status file supplied to this process: it lets an isolated/preview cockpit build
    // monitor and repair the selected workspace instead of accidentally acting on the directory
    // that happens to contain the exe. Normal installs still resolve exactly as before because
    // status.json lives in <repo>\.fleet. Fall back to exe\.. for custom/non-.fleet paths.
    string RepoRoot()
    {
        try
        {
            string stateDir = Path.GetDirectoryName(Path.GetFullPath(_statusPath));
            if (string.Equals(Path.GetFileName(stateDir), ".fleet", StringComparison.OrdinalIgnoreCase))
                return Path.GetFullPath(Path.Combine(stateDir, ".."));
        }
        catch (Exception) { }
        return Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
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
        if (s == HealthState.Red) return Theme.Br(Theme.Danger(_dark));
        if (s == HealthState.Yellow) return Theme.Br(Theme.Warning(_dark));
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
                _healthDotWrap[i].ToolTip = T(_healthKeys[i]) + ": " + detail + "\n"
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
        // Clear the stale hint text once everything the strip knows about is healthy again (not
        // mid-fix): RunFix's note() writes _fixNote.Text once and nothing else used to clear it,
        // so "run start_all.bat"-style residue could persist forever after the stack recovered.
        // This runs on the UI thread already (ApplyHealthToUi's documented contract), so no
        // Dispatcher marshal is needed here (mirrors the rest of this method).
        if (!anyBad && !_fixRunning && _fixNote != null && _fixNote.Text.Length > 0)
            _fixNote.Text = "";
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

    // One full infra sweep. Writes results into _health under _healthLock.
    void PollHealthOnce()
    {
        DateTime now = DateTime.UtcNow;

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
        string srvBody = HttpBody("http://127.0.0.1:8000/health", 3500);
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
            SetDot(0, HealthState.Red, T("hs_srv_detail_bad"), now);
        else if (authStorm)
            SetDot(0, HealthState.Yellow,
                   T("hs_srv_detail_auth") + " (" + authFails + ")", now);
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
                SetDot(1, HealthState.Red, T("hs_tun_detail_bad"), now);
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
            // WHERE IT LOOKED. A panel that reports "nothing queued" without saying where
            // it looked cannot be checked against the queue on disk -- which is the one
            // comparison this publication exists to make possible.
            sb.Append(",\"queue_dir\":\"").Append(JsonEscape(TasksDir())).Append('"');
            sb.Append(",\"queued_count\":").Append(qj.Count.ToString(inv));
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
                if (dot == 3 && st != HealthState.Red) continue;
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

    // Sign-in and Agent, from what the last capture established rather than from tabs.
    //
    //   sign-in   grey    no capture on record and no run wanting one
    //             green   the last capture worked and its token still has life
    //             amber   the last capture failed for a reason a person cannot fix, or the
    //                     token has expired while a run is live
    //             red     the last capture failed because sign-in is needed
    //
    //   agent     grey    no run
    //             green   run live, the route is open and the template names an agent
    //             amber   run live and the route is CLOSED -- every worker is on a tab,
    //                     which works and costs several times more. Not an error, and not
    //                     invisible either: amber is exactly 'working, worth knowing'.
    //             red     run live and no agent was ever established
    void UpdateCaptureDots(DateTime now)
    {
        Dictionary<string, object> cap = ReadCaptureStatus();
        bool live = RunIsLive();

        if (cap == null)
        {
            // NO RECORD. Grey while idle -- a system that has not captured yet is not
            // unhealthy. Amber during a run, because then the evidence IS expected.
            SetDot(3, live ? HealthState.Yellow : HealthState.Gray,
                   T(live ? "hs_signin_unknown_live" : "hs_signin_gray"), now);
            // AMBER FIRST, by this file's own rule two lines up: evidence that is expected
            // and missing is not evidence of failure. The sign-in dot gets amber in exactly
            // this situation on the line above; the agent dot jumped to red, and red here
            // drives a reconnect. A first run on a fresh checkout has no capture record at
            // all, and neither does one where the status writer swallowed an exception.
            SetDot(4, live ? HealthState.Yellow : HealthState.Gray,
                   T(live ? "hs_agent_unknown_live" : "hs_agent_gray"), now);
            return;
        }

        bool ok = TruthyField(cap, "ok");
        double expiresAt = NumberField(cap, "expires_at");
        string kind = StringField(cap, "kind");
        // THE FIELD IS `at`. relay/capture_status.py's FIELDS tuple is ("ok", "at",
        // "expires_at", ...) and there has never been a "ts" in it -- so capTs was 0 on every
        // read, capFresh below was false on every read, and the RED branch that needs it has
        // never once been taken. The sign-in dot was structurally incapable of turning red:
        // a capture that failed for a sign-in reason five seconds ago showed the same amber
        // as one that failed last week, which is precisely the distinction the comment below
        // spends a paragraph arguing for.
        //
        // `ts` is still read as a fallback so a file written by any other producer, or an
        // older one, keeps working.
        double capTs = NumberField(cap, "at");
        if (capTs <= 0) capTs = NumberField(cap, "ts");
        string gptId = StringField(cap, "gpt_id");
        double nowEpoch = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        double lifeLeft = expiresAt - nowEpoch;

        // SIGN-IN. A live token is positive evidence. An expired one is NOT automatically
        // red: the next capture may well succeed, and a dot that stays red for hours after a
        // finished run is a dot people stop reading. Expiry only matters while a run wants
        // a token.
        // AND IT HAS TO BE RECENT. capture_status.json keeps the last capture whether it was a
        // minute or a day ago, so an old sign-in failure stayed red indefinitely -- driving a
        // repair that relaunches the browser HEADED, for a refusal that may long since have
        // resolved. Evidence about the past is not evidence about now.
        double capAgeS = (capTs > 0) ? (nowEpoch - capTs) : -1;
        // HOW LONG A BROKEN SIGN-IN MAY GO UNNOTICED IS OUR REQUIREMENT, NOT THE ISSUER'S.
        // Two wrong answers were tried here before this one.
        //
        // A flat 30 minutes was never measured against anything. The token on disk when this
        // was written lived 3,848s (at=1789532602, expires_at=1789536451, about 64 minutes)
        // and captures happen roughly once per token lifetime, so the back half of EVERY
        // healthy cycle showed amber "unverified" with nothing wrong -- and a dot that is
        // amber through half of normal operation is one people stop reading, which is the
        // failure this file argues against repeatedly, reached from the other side.
        //
        // Then the window was taken from the record as (expires_at - at) and called "one
        // capture cycle". It is not one: now - at <= expires_at - at reduces to
        // now <= expires_at, which is "green while the token has not expired" -- redundant
        // with the lifeLeft > 0 already on this branch, and worse, it hands the detection
        // delay to whoever issues the tokens. If their lifetime became 24 hours, a capture
        // path that died would stay green for 24 hours and nothing here would have changed.
        //
        // So: a constant, because the requirement is ours -- but one with a measurement under
        // it. 5,400s is 1.4 token lifetimes as measured on this machine, which leaves room
        // for a capture that comes slightly late without keeping green alive across a whole
        // missed cycle. Expiry is a separate and additional ceiling (lifeLeft > 0 below), so
        // green needs both: a token that has not run out AND evidence no older than this.
        // Neither establishes that the session was not revoked in between; nothing in a
        // record of past captures can.
        bool capFresh = capAgeS >= 0 && capAgeS <= SIGNIN_EVIDENCE_MAX_AGE_S;
        if (!ok && string.Equals(kind, "signin", StringComparison.OrdinalIgnoreCase) && capFresh)
            SetDot(3, HealthState.Red, T("hs_signin_bad"), now);
        else if (!ok && string.Equals(kind, "signin", StringComparison.OrdinalIgnoreCase))
            SetDot(3, HealthState.Yellow, T("hs_signin_old"), now);
        else if (!ok)
            SetDot(3, HealthState.Yellow, T("hs_signin_failed_other"), now);
        else if (lifeLeft > 0 && capFresh)
            SetDot(3, HealthState.Green, T("hs_signin_ok"), now);
        else if (lifeLeft > 0)
            // THE RULE THE PARAGRAPH ABOVE ARGUES FOR, APPLIED TO SUCCESSES TOO. "Evidence
            // about the past is not evidence about now" was written for failures and only
            // ever gated failures: an ok=true capture with a far-future expires_at read as
            // green forever, however old the record, because nothing re-verifies the session.
            // A token can be revoked by tenant policy or a sign-out elsewhere and this dot
            // would not notice until some later capture happened to fail.
            //
            // Amber while a run is live, because then the evidence IS expected and its
            // absence matters. Grey when idle, because nothing is asking and an old record is
            // simply not current evidence -- painting an idle machine amber would be the
            // false-alarm half of the same mistake.
            SetDot(3, live ? HealthState.Yellow : HealthState.Gray,
                   T(live ? "hs_signin_old_ok" : "hs_signin_gray_old"), now);
        else if (live)
            SetDot(3, HealthState.Yellow, T("hs_signin_stale"), now);
        else
            SetDot(3, HealthState.Gray, T("hs_signin_gray_expired"), now);

        // AGENT. The template naming an agent is a STRONGER guarantee than the tab sniffing
        // it replaces: a capture whose request names no agent raises NotAnAgentSurface and
        // produces no template at all, so a silent bind to the default Copilot -- no
        // connectors, no tenant grounding, and a fluent answer -- cannot happen unnoticed.
        if (!live)
            SetDot(4, HealthState.Gray, T("hs_agent_gray"), now);
        else if (RouteState() == ROUTE_UNKNOWN)
            // Amber, not green: the route's own record could not be read, so whether workers
            // are on tabs is unknown, and the binding check below would be answering a
            // different question than the one being asked.
            SetDot(4, HealthState.Yellow, T("hs_agent_route_unknown"), now);
        else if (RouteState() == ROUTE_CLOSED)
        {
            // THE CANNED-ANSWER SNIFF SURVIVES HERE AND NOWHERE ELSE. It used to decide the
            // dot's colour, which made a judgement about ONE TURN'S QUALITY into a statement
            // about infrastructure. It still means something while workers are on tabs, so it
            // is demoted to a note on the amber rather than deleted.
            string tail = NewestAssistantText();
            string note = T("hs_agent_tabs");
            if (tail != null && LooksLikeCannedNonAnswer(tail)) note += T("hs_agent_canned");
            SetDot(4, HealthState.Yellow, note, now);
        }
        else if (FleetAgentIsBound())
            SetDot(4, HealthState.Green, T("hs_agent_ok"), now);
        else if (!string.IsNullOrEmpty(gptId))
            // THIS FALLBACK IS THE FIELD THE CHECK ABOVE EXISTS TO DISTRUST. Read
            // FleetAgentIsBound's own comment: capture_status.json's gpt_id is "the LAST
            // capture on ANY surface", the route also captures for side agents, and "the last
            // event about somebody else is not evidence about you". Then this line took that
            // same field as sufficient for GREEN, reintroducing precisely what the primary
            // check was built to avoid.
            //
            // It is kept, because a non-empty gpt_id is not nothing -- some surface did bind
            // to some agent. It is amber, because it is not evidence about THIS one.
            SetDot(4, HealthState.Yellow, T("hs_agent_other_surface"), now);
        else
            SetDot(4, HealthState.Red, T("hs_agent_bad"), now);
    }

    // Is the FLEET'S OWN surface bound to an agent?
    //
    // NOT capture_status.json's gpt_id, which is the LAST capture on ANY surface. The route
    // also captures for side agents -- a researcher or an analyst, on conversation-specific
    // URLs -- and one of those was observed writing an empty gpt_id, which would have turned
    // this dot red while the fleet's own agent was working perfectly. The last event about
    // somebody else is not evidence about you.
    //
    // The per-surface fact is already on disk: relay/profile_token.py caches one request
    // template per agent surface, named by a hash of its URL, and refuses to return one that
    // names no agent. Its existence IS the binding.
    bool FleetAgentIsBound()
    {
        try
        {
            string url = EnvValue("MCP_FLEET_AGENT_URL");
            if (string.IsNullOrEmpty(url)) url = EnvValue("MCP_IMPL_AGENT_URL");
            if (string.IsNullOrEmpty(url)) return false;
            string path = Path.Combine(RepoRoot(), ".fleet", "templates",
                                       "template_" + Sha256Prefix(url) + ".json");
            if (!File.Exists(path)) return false;
            // EXISTENCE IS NOT THE BINDING, and the comment above used to say it was. The
            // module that writes this file refuses to return one older than
            // TEMPLATE_MAX_AGE_S (relay/profile_token.py), and refuses one whose gpt_id is
            // empty (load_template's "a template without an agent is not a cache hit"). This
            // dot checked neither, so it could report a binding the route itself would evict
            // on first use -- and would go on reporting it, because the eviction only happens
            // when a capture runs, about once per token lifetime.
            //
            // Measured when this was written: .fleet/templates held one template 211.7 hours
            // old, 8.8 days against a 24 hour cap. Dot 3 was given an explicit max age for
            // exactly this reason ("evidence about the past is not evidence about now"); dot
            // 4 was not.
            var tpl = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8))
                      as Dictionary<string, object>;
            if (tpl == null) return false;
            double savedAt = NumberField(tpl, "ts");
            double ageS = NowUnix() - savedAt;
            if (savedAt <= 0 || ageS > TEMPLATE_MAX_AGE_S) return false;
            var query = tpl.ContainsKey("query") ? tpl["query"] as Dictionary<string, object>
                                                 : null;
            // Non-EMPTY, not merely present. The old substring test asked whether the five
            // characters "gptId" appeared, which is a question about the file's spelling.
            return !string.IsNullOrEmpty(StringField(query, "gptId"));
        }
        catch (Exception) { return false; }
    }

    // The same 16 hex characters relay/profile_token.py names its cache files with. If these
    // two ever disagree the lookup finds nothing and the dot reports "not bound" for ever --
    // a silent zero -- so the algorithm is stated in both places rather than assumed.
    //: A COPY OF relay/profile_token.py's TEMPLATE_MAX_AGE_S, and copies drift, so a test
    //: fails if the two stop agreeing. Stated here rather than read from the environment
    //: because the cockpit must not report a binding on terms looser than the route's.
    const double TEMPLATE_MAX_AGE_S = 24 * 3600;

    static string Sha256Prefix(string s)
    {
        using (var sha = System.Security.Cryptography.SHA256.Create())
        {
            byte[] hash = sha.ComputeHash(System.Text.Encoding.UTF8.GetBytes(s ?? ""));
            var sb = new StringBuilder();
            for (int i = 0; i < 8; i++) sb.Append(hash[i].ToString("x2"));
            return sb.ToString();
        }
    }

    Dictionary<string, object> ReadCaptureStatus()
    {
        try
        {
            string path = Path.Combine(RepoRoot(), ".fleet", "capture_status.json");
            if (!File.Exists(path)) return null;
            return _js.DeserializeObject(File.ReadAllText(path)) as Dictionary<string, object>;
        }
        catch (Exception) { return null; }
    }

    // Has the route's one-way breaker tripped DURING THIS RUN? Read from the route's own
    // append-only record. Scanned from the run's start, because a close is per-run: the route
    // is rebuilt with each coordinator, and a close from yesterday says nothing about now.
    //: What RouteState() found. UNKNOWN exists because the alternative is a check that
    //: reports a healthy open route when it could not read the file at all.
    const int ROUTE_OPEN = 0;
    const int ROUTE_CLOSED = 1;
    const int ROUTE_UNKNOWN = 2;

    int RouteState()
    {
        try
        {
            string path = Path.Combine(RepoRoot(), ".fleet", "socket_route.jsonl");
            // NO FILE IS A REAL ANSWER, not a failure to read one: nothing has ever closed
            // the route on this checkout. Only a file that exists and cannot be understood
            // is unknown.
            if (!File.Exists(path)) return ROUTE_OPEN;
            string stamp = RunStartedLocal().ToString("yyyy-MM-ddTHH:mm:ss");
            // THE LAST ROUTE EVENT DECIDES, NOT THE FIRST CLOSE. A closed route can now be
            // reopened mid-run (relay/route_reopen.py), and this used to answer "closed" if
            // it found any close at all since the run began -- so a route that came back
            // after a passing fault stayed amber, and the dot described a state the fleet
            // had already left. Both events carry the same timestamp key, so the scan just
            // keeps the newest one it sees.
            bool closed = false;
            int routeLines = 0, parsed = 0;
            foreach (string line in File.ReadLines(path))
            {
                bool isClose = line.IndexOf("route_closed", StringComparison.Ordinal) >= 0;
                bool isOpen = line.IndexOf("route_reopened", StringComparison.Ordinal) >= 0;
                if (!isClose && !isOpen) continue;
                routeLines++;
                // The record is written by json.dumps, which puts a space after the colon:
                //   {"ts": 1787270671.84, "at": "2026-08-21T09:04:31", "event": "route_closed"}
                // Matching without the space finds nothing, and finding nothing here means the
                // dot reports an open route for ever -- a silent zero, which is how a check
                // fails open. The marker is taken from a real line rather than composed.
                const string AtKey = "\"at\": \"";
                int i = line.IndexOf(AtKey, StringComparison.Ordinal);
                if (i < 0) continue;
                int from = i + AtKey.Length;
                if (from + 19 > line.Length) continue;
                string at = line.Substring(from, 19);
                parsed++;
                if (string.CompareOrdinal(at, stamp) >= 0) closed = isClose;
            }
            // ROUTE EVENTS THAT NONE OF THIS COULD READ. The comment above explains why the
            // marker is copied from a real line rather than composed; this is what happens
            // when that copy stops matching anyway. Saying "open" there is the silent zero
            // the comment warns about, stated and then not guarded against.
            if (routeLines > 0 && parsed == 0) return ROUTE_UNKNOWN;
            return closed ? ROUTE_CLOSED : ROUTE_OPEN;
        }
        catch (Exception) { }
        // An unreadable file is not an open route. It is nothing at all, and the dot now has
        // somewhere to put that.
        return ROUTE_UNKNOWN;
    }

    DateTime RunStartedLocal()
    {
        try
        {
            double started = NumberField(ReadStatus(), "started");
            if (started > 0)
                return new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(started).ToLocalTime();
        }
        catch (Exception) { }
        return DateTime.Now.AddHours(-24);
    }

    static bool TruthyField(Dictionary<string, object> d, string key)
    {
        object v;
        if (d == null || !d.TryGetValue(key, out v) || v == null) return false;
        if (v is bool) return (bool)v;
        return string.Equals(Convert.ToString(v), "True", StringComparison.OrdinalIgnoreCase);
    }

    static double NumberField(Dictionary<string, object> d, string key)
    {
        object v;
        if (d == null || !d.TryGetValue(key, out v) || v == null) return 0;
        try { return Convert.ToDouble(v); } catch (Exception) { return 0; }
    }

    static string StringField(Dictionary<string, object> d, string key)
    {
        object v;
        if (d == null || !d.TryGetValue(key, out v) || v == null) return "";
        return Convert.ToString(v);
    }
    // A COPY OF relay/relay_fleet.py's CANNED_NONANSWER_MARKERS, and copies drift. The
    // escalation line was in a real run for hours before either list had it; a test now
    // fails if the two stop agreeing, because a cockpit that cannot recognise what the
    // relay treats as a non-answer shows a worker as healthy while the relay re-queues it.
    static readonly string[] _cannedNonAnswer = {
        "それに応答できませんでした", "I couldn't respond to that", "I can't respond to that",
        "担当者へのエスカレーションが構成されていません",
    };
    static bool LooksLikeCannedNonAnswer(string s)
    {
        if (string.IsNullOrEmpty(s)) return false;
        foreach (string m in _cannedNonAnswer) if (s.IndexOf(m, StringComparison.OrdinalIgnoreCase) >= 0) return true;
        return false;
    }

    // 5) Tool: read the bridge's self-probe result, written independently by another component
    // (bridge/copilot_bridge.py + tools/tool_probe.py) roughly every ~10 min. This file only READS
    // it -- never writes .fleet/tool_probe.json. Shape: {"ts":<epoch float>,"ok":<bool>,
    // "kind":"answer"|"consent_card"|"canned_fallback"|"timeout"|"agent_unreachable"|"error",
    // "detail":<str>}.
    //   GRAY  -- file has never existed (probe disabled / MCP_TOOL_PROBE_SEC=0 on this machine).
    //            Deliberately NOT red: a new/unconfigured feature must never read as an outage.
    //   RED   -- file missing after having looked (can't happen here since we check Exists first,
    //            kept as a safety fallback) OR stale (>20 min since ts -- the probe itself isn't
    //            running) OR the probe failed with nothing coming back at all.
    //   GREEN -- ok==true AND fresh (<20 min old).
    //   YELLOW-- consent_card/canned_fallback, or the probe failed while "alive" says a reply
    //            DID arrive. Red is reserved for silence: a failed probe on a chat that is
    //            answering normally used to paint this dot red, and a red dot is read as
    //            "everything is broken" by every user who sees it.
    void PollToolProbeOnce(DateTime now)
    {
        string path = Path.Combine(RepoRoot(), ".fleet", "tool_probe.json");
        if (!File.Exists(path))
        {
            // GRAY ONLY WHEN THE PROBE IS ACTUALLY SWITCHED OFF. "probe disabled", "the bridge
            // never started" and "the probe writer is broken" all produced the same grey dot,
            // and grey means "no evidence expected" -- so two of those three were reported as
            // nothing to see. The setting is readable, so the three can be told apart.
            string probeSec = EnvValue("MCP_TOOL_PROBE_SEC");
            bool disabled = probeSec == "0";
            SetDot(5, disabled ? HealthState.Gray : HealthState.Yellow,
                   T(disabled ? "hs_tool_detail_none" : "hs_tool_detail_never"), now);
            return;
        }
        try
        {
            var o = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
            double ts = (o != null && o.ContainsKey("ts")) ? Convert.ToDouble(o["ts"]) : 0.0;
            bool ok = (o != null && o.ContainsKey("ok")) && Convert.ToBoolean(o["ok"]);
            string kind = (o != null && o.ContainsKey("kind")) ? Convert.ToString(o["kind"]) : "";
            double ageMin = ts > 0 ? (NowUnix() - ts) / 60.0 : double.MaxValue;
            string ageTxt = ts > 0 ? AgeMinutesText(ageMin) : T("hs_never");
            // A PROBE IN FLIGHT NO LONGER REPLACES THE VERDICT. tools/tool_probe.py used to
            // write kind="checking" over the whole record, so for the 30-180s of a real round
            // trip there was no measured result at all and this dot changed colour every
            // cycle, on a schedule, with nothing wrong. A dot that does that is one people
            // stop reading -- it was reported as a fault, correctly. The transitional state is
            // now a flag beside the last verdict, and the colour keeps reporting what was last
            // actually measured.
            double probingAgeMin = double.MaxValue;
            if (o != null && o.ContainsKey("probing_since"))
            {
                double ps = Convert.ToDouble(o["probing_since"]);
                if (ps > 0) probingAgeMin = (NowUnix() - ps) / 60.0;
            }
            // Only while it is plausibly still running. A probe that started an hour ago and
            // never recorded a result is not "in progress", it is a prober that died.
            bool probing = probingAgeMin < 20.0;

            // TWO TOOL PATHS, AND THIS DOT ONLY EVER WATCHED ONE. Everything above comes from
            // .fleet/tool_probe.json, written solely by the BRIDGE's idle self-probe on its
            // own Edge profile (CDP :9223). Fleet workers call tools over a different
            // transport entirely, and on 2026-09-16 this dot was red -- truthfully, the bridge
            // agent had lost its tool list -- while fleet workers completed 84 tool calls in
            // an hour and a goal finished DONE with the refuter upholding it. A dot labelled
            // "Tool" showed red and tool access was fine.
            //
            // fleet_tool_ok comes from /health (tools/fleet_tool_health.py), derived from the
            // ledger of real calls rather than from a probe. "" or "null" means no calls
            // lately, which is NOT evidence of anything and must not colour the dot.
            // Read through the age gate, not from the field directly: HealthField's contract is
        // that "" means "no evidence", and a stale body has exactly as much evidence in it.
        string fleetTool = (_lastHealthBodyAt > 0
                            && NowUnix() - _lastHealthBodyAt <= HEALTH_BODY_MAX_AGE_S)
                         ? HealthField(_lastHealthBody, "fleet_tool_ok") : "";
            bool fleetWorking = fleetTool == "true";
            bool fleetFailing = fleetTool == "false";

            if (fleetWorking)
            {
                // CURRENT OPERABILITY WINS THE COLOUR. The Fleet is the path this cockpit is
                // supervising. If real Fleet calls are succeeding, Tool is green. A degraded
                // bridge/chat probe remains visible in the detail, but must not turn the Fleet
                // traffic light amber while the work path is demonstrably healthy.
                string detail = ok
                    ? (ageTxt + " " + T("hs_tool_detail_ok"))
                    : (ageTxt + " " + T("hs_tool_detail_bridge_only_down"));
                if (ok && probing) detail += " / " + T("hs_tool_detail_checking");
                SetDot(5, HealthState.Green, detail, now);
            }
            else if (ok && fleetFailing)
                // The bridge can call tools and the fleet cannot. Green here would hide the
                // failure of the path that does the work.
                SetDot(5, HealthState.Yellow, ageTxt + " " + T("hs_tool_detail_fleet_down"), now);
            else if (ageMin >= 20.0)
                SetDot(5, HealthState.Red, ageTxt + " " + T("hs_tool_detail_stale"), now);
            else if (kind == "checking" || kind == "starting")
                // A record written by an older prober, which still overwrote the verdict.
                SetDot(5, HealthState.Checking, ageTxt + " " + T("hs_tool_detail_checking"), now);
            else if (ok && probing)
                SetDot(5, HealthState.Green,
                       ageTxt + " " + T("hs_tool_detail_ok") + " / " + T("hs_tool_detail_checking"),
                       now);
            else if (ok)
                SetDot(5, HealthState.Green, ageTxt + " " + T("hs_tool_detail_ok"), now);
            else if (kind == "consent_card" || kind == "canned_fallback")
                SetDot(5, HealthState.Yellow, ageTxt + " " + T("hs_tool_detail_consent"), now);
            else if (o != null && o.ContainsKey("alive") && Convert.ToBoolean(o["alive"]))
                // The chat answered, it just did not complete the probe's round trip. Calling
                // that "no response, reconnect needed" in red told everyone the stack was down
                // while it was serving turns perfectly well -- the one reading a user acts on.
                SetDot(5, HealthState.Yellow, ageTxt + " " + T("hs_tool_detail_no_tool"), now);
            else   // timeout | agent_unreachable | genuinely silent | unrecognized kind
                SetDot(5, HealthState.Red, ageTxt + " " + T("hs_tool_detail_down"), now);
        }
        catch (Exception)
        {
            // Malformed/partial JSON (e.g. read mid-write by the bridge) -- treat as down, not gray,
            // since the file DOES exist (the feature is active, just unreadable right now).
            SetDot(5, HealthState.Red, T("hs_tool_detail_down"), now);
        }
    }

    // "N分前" / "N min ago" -- small formatter local to the Tool dot's tooltip; not routed through
    // T() because it embeds a number (T() keys are static lookups, no interpolation).
    string AgeMinutesText(double ageMin)
    {
        int m = (int)Math.Max(0, Math.Round(ageMin));
        return (_lang == 0) ? (m + "分前") : (m + " min ago");
    }

    // Read the newest .fleet\transcripts\*.jsonl and return the text of its last assistant turn,
    // or null if none. Fully guarded, cheap (reads one file, scans lines). Runs on the poll thread.
    string NewestAssistantText()
    {
        try
        {
            string dir = Path.GetFullPath(Path.Combine(
                AppDomain.CurrentDomain.BaseDirectory, "..", ".fleet", "transcripts"));
            if (!Directory.Exists(dir)) return null;
            string newest = null; DateTime best = DateTime.MinValue;
            foreach (string f in Directory.GetFiles(dir, "*.jsonl"))
            {
                DateTime wt = File.GetLastWriteTimeUtc(f);
                if (wt > best) { best = wt; newest = f; }
            }
            if (newest == null) return null;
            string last = null;
            foreach (string line in File.ReadLines(newest))
            {
                if (line.IndexOf("\"role\"", StringComparison.Ordinal) < 0) continue;
                if (line.IndexOf("\"assistant\"", StringComparison.Ordinal) < 0) continue;
                last = line;   // keep the last assistant line
            }
            if (last == null) return null;
            // extract the "text" field value (simple, tolerant): find "text":" ... unescaped close
            int ti = last.IndexOf("\"text\"", StringComparison.Ordinal);
            if (ti < 0) return "";
            int c = last.IndexOf(':', ti); if (c < 0) return "";
            int q = last.IndexOf('"', c + 1); if (q < 0) return "";
            var sb = new StringBuilder();
            for (int i = q + 1; i < last.Length; i++)
            {
                char ch = last[i];
                if (ch == '\\' && i + 1 < last.Length) { i++; char n = last[i]; sb.Append(n == 'n' ? '\n' : n); continue; }
                if (ch == '"') break;
                sb.Append(ch);
            }
            return sb.ToString();
        }
        catch (Exception) { return null; }
    }

    void SetDot(int i, HealthState s, string detail, DateTime whenUtc)
    {
        lock (_healthLock) { _health[i].State = s; _health[i].Detail = detail; _health[i].Checked = whenUtc; }
    }

    // Extract the T_.../P_... agent id from the configured agent URL in .env. The URL may be
    // '.../chat/?titleId=T_xxx' (deep link) OR '.../chat/agent/T_xxx' — both carry the same id.
    // Matched later (case-insensitively) inside any tab url, including /chat/agent/<id> conversation
    // forms. Returns "" if no agent URL is configured.
    string ExtractAgentMarker()
    {
        string url = EnvValue("MCP_FLEET_AGENT_URL");
        if (string.IsNullOrEmpty(url)) url = EnvValue("MCP_IMPL_AGENT_URL");
        return AgentIdFromUrl(url);
    }
    static string AgentIdFromUrl(string url)
    {
        if (string.IsNullOrEmpty(url) || url == null) return "";
        // titleId=<id>
        int ti = url.IndexOf("titleId=", StringComparison.OrdinalIgnoreCase);
        if (ti >= 0)
        {
            string tail = url.Substring(ti + 8);
            return TrimId(tail);
        }
        // /agent/<id>
        int ai = url.IndexOf("/agent/", StringComparison.OrdinalIgnoreCase);
        if (ai >= 0)
        {
            string tail = url.Substring(ai + 7);
            return TrimId(tail);
        }
        return "";
    }
    // Cut an id token at the first url separator (&, ?, /, #, whitespace). Keeps '.', '-', '_'
    // (agent ids like 'P_552e6eda-...-....dr_work' and 'T_02140b8c-f551-...' contain those).
    static string TrimId(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder();
        foreach (char c in s)
        {
            if (c == '&' || c == '?' || c == '/' || c == '#' || char.IsWhiteSpace(c)) break;
            sb.Append(c);
        }
        return sb.ToString();
    }

    // THE LOGIN-WALL PREDICATE IS GONE WITH THE TAB LIST IT READ. It answered "is one of the
    // open tabs a sign-in page", and under the socket route there are no tabs to ask about --
    // the health strip judges sign-in from what the last capture established instead. The
    // failure mode it leaves behind is worth naming: a predicate over an empty list returns
    // false, so the dot went green because nothing was there rather than because sign-in
    // worked. See UpdateCaptureDots.


    // ── .env reader (utf-8, tolerate BOM) ───────────────────────────────────────────
    // The .env lives at the REPO ROOT (one level up from ...\ui), same file doctor.ps1 reads.
    // Cached by mtime so a poll every 15s doesn't re-read from disk each time.
    Dictionary<string, string> _envCache;
    long _envMtime = -1;
    string EnvValue(string key)
    {
        try
        {
            string envPath = Path.Combine(RepoRoot(), ".env");
            if (!File.Exists(envPath)) { _envCache = null; return ""; }
            long m = File.GetLastWriteTimeUtc(envPath).Ticks;
            if (_envCache == null || m != _envMtime)
            {
                var map = new Dictionary<string, string>();
                // UTF8 with BOM tolerated (new UTF8Encoding detects+strips a leading BOM).
                foreach (string raw in File.ReadAllLines(envPath, new UTF8Encoding(false)))
                {
                    string ln = raw;
                    if (ln.Length > 0 && ln[0] == '﻿') ln = ln.Substring(1);   // stray BOM guard
                    ln = ln.Trim();
                    if (ln.Length == 0 || ln[0] == '#') continue;
                    int eq = ln.IndexOf('=');
                    if (eq <= 0) continue;
                    string k = ln.Substring(0, eq).Trim();
                    string v = ln.Substring(eq + 1).Trim();
                    if (k.Length > 0) map[k] = v;
                }
                _envCache = map;
                _envMtime = m;
            }
            string val;
            if (_envCache != null && _envCache.TryGetValue(key, out val)) return val;
        }
        catch (Exception) { }
        return "";
    }

    // Enable TLS 1.2 process-wide: the tunnel is HTTPS and .NET Framework's default
    // protocol set can omit TLS 1.2, so the devtunnels handshake fails (false-red tunnel
    // dot) while plain-HTTP localhost is fine. Called once from the HTTP helpers.
    static bool _tlsReady = false;
    static void EnsureTls()
    {
        if (_tlsReady) return;
        try { ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072; } catch (Exception) { }
        _tlsReady = true;
    }

    // GET a URL; true iff it returns HTTP 200. Short timeout, fully guarded.
    // Loopback URLs bypass the system proxy: on corporate machines the PAC/proxy can
    // swallow 127.0.0.1 requests, turning a healthy local server into a false-red dot.
    // THE BODY WAS ALWAYS THERE AND NOBODY READ IT. /health returns status, auth_fail_10m,
    // tool_ok and fleet_tool_ok (main.py:296-323), and main.py's own docstring says the
    // endpoint is deliberately non-blocking so it answers 200 even while auth is failing and
    // every tool call is dying. HttpOk discards the body, so the Server dot was green in
    // exactly the states the payload was written to expose. Audited 2026-09-16: no consumer
    // of /health anywhere in the repository parsed the body -- not this file, not doctor.ps1,
    // not supervisor.ps1, not the relay.
    //
    // Returns the body on a 200, or null on any failure, so a caller can tell "unreachable"
    // from "reachable and complaining". No JSON parser is pulled in for this: the payload is
    // flat and the two questions asked of it are substring-shaped.
    static string HttpBody(string url, int timeoutMs)
    {
        try
        {
            EnsureTls();
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "GET";
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.AllowAutoRedirect = true;
            if (url.Contains("127.0.0.1") || url.Contains("localhost")) req.Proxy = null;
            else if (req.Proxy != null) req.Proxy.Credentials = CredentialCache.DefaultCredentials;
            using (var resp = (HttpWebResponse)req.GetResponse())
            {
                if (resp.StatusCode != HttpStatusCode.OK) return null;
                using (var sr = new StreamReader(resp.GetResponseStream()))
                    return sr.ReadToEnd();
            }
        }
        catch (Exception) { return null; }
    }

    // Pull one flat JSON value out without a parser. Returns "" when absent, which every
    // caller must treat as "no evidence" rather than as a negative -- /health omits fields it
    // has nothing to say about, and an omission is not a failure.
    static string HealthField(string body, string key)
    {
        if (string.IsNullOrEmpty(body)) return "";
        int i = body.IndexOf("\"" + key + "\"");
        if (i < 0) return "";
        int c = body.IndexOf(':', i);
        if (c < 0) return "";
        int e = c + 1;
        while (e < body.Length && body[e] != ',' && body[e] != '}') e++;
        return body.Substring(c + 1, e - c - 1).Trim().Trim('"');
    }

    static bool HttpOk(string url, int timeoutMs)
    {
        try
        {
            EnsureTls();
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "GET";
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.AllowAutoRedirect = true;
            if (url.Contains("127.0.0.1") || url.Contains("localhost")) req.Proxy = null;
            else if (req.Proxy != null) req.Proxy.Credentials = CredentialCache.DefaultCredentials;
            using (var resp = (HttpWebResponse)req.GetResponse())
                return resp.StatusCode == HttpStatusCode.OK;
        }
        catch (Exception) { return false; }
    }

    // GET a URL; return the body string, or null on any failure (unreachable / non-200 / timeout).
    static string HttpGetBody(string url, int timeoutMs)
    {
        try
        {
            EnsureTls();
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "GET";
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            if (url.Contains("127.0.0.1") || url.Contains("localhost")) req.Proxy = null;
            using (var resp = (HttpWebResponse)req.GetResponse())
            {
                if (resp.StatusCode != HttpStatusCode.OK) return null;
                using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                    return sr.ReadToEnd();
            }
        }
        catch (Exception) { return null; }
    }

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
