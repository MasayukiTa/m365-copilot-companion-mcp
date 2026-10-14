// FleetCockpit.RunControls.cs -- run controls and banners: pause/stop/autoscale, liveness, max-tabs, cap/rejection banners.
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

    // Fleet-wide controls: Pause/Resume toggle + Stop-all. Both send their own single-key
    // command file through FleetCommands.Write, so neither can clobber a queued close/steer/add.
    // fleet_runner._drain_commands consumes {"pause":bool}/{"stop":true} each sweep.
    UIElement FleetControls()
    {
        var group = new StackPanel(); group.Orientation = Orientation.Horizontal;
        group.VerticalAlignment = VerticalAlignment.Center; group.Margin = new Thickness(0, 0, 12, 0);

        _pauseBtn = new Button();
        _pauseBtn.ToolTip = _lang == 0 ? "新規ターン/タブを止めて凍結（再開で続行・状態は保持）" : "Freeze: no new turns/tabs (resume continues; state kept)";
        System.Windows.Automation.AutomationProperties.SetName(_pauseBtn, _lang == 0 ? "一時停止" : "Pause");
        _pauseBtn.Cursor = Cursors.Hand; _pauseBtn.BorderThickness = new Thickness(1);
        _pauseBtn.Width = 36; _pauseBtn.Height = HeaderCtrlH; _pauseBtn.Padding = new Thickness(0);
        _pauseBtn.Margin = new Thickness(0, 0, 8, 0);
        _pauseBtn.VerticalAlignment = VerticalAlignment.Center;
        // FlatButtonTemplate makes Background=BtnBg actually render (the default Aero template
        // paints its own light gradient, making a light-colored icon invisible in dark mode).
        _pauseBtn.Template = FlatButtonTemplate();
        _pauseIcon = new System.Windows.Shapes.Path { Stretch = Stretch.Uniform };
        {
            var vb = new Viewbox();
            vb.Width = 16; vb.Height = 16;
            vb.Stretch = Stretch.Uniform;
            vb.HorizontalAlignment = HorizontalAlignment.Center;
            vb.VerticalAlignment = VerticalAlignment.Center;
            vb.Child = _pauseIcon;
            _pauseBtn.Content = vb;
        }
        _pauseBtn.Click += delegate
        {
            // Pause only means something to a LIVE fleet. With no live consumer the command would
            // sit unread in commands.d/ while the label lied "Resume" -- so do nothing then (the
            // button is also disabled per-tick by RefreshPauseEnabled, this is belt-and-suspenders).
            if (!RunIsLive()) { if (_paused) { _paused = false; PaintPause(); } return; }
            _paused = !_paused;
            SendCommand(Cmd1("pause", _paused));
            PaintPause();
        };
        group.Children.Add(_pauseBtn);

        _stopBtn = new Button();
        _stopBtn.ToolTip = _lang == 0 ? "全ワーカーを停止して走行を終了" : "Cancel every worker and end the run";
        System.Windows.Automation.AutomationProperties.SetName(_stopBtn, _lang == 0 ? "停止" : "Stop");
        _stopBtn.Cursor = Cursors.Hand; _stopBtn.BorderThickness = new Thickness(1);
        _stopBtn.Width = 36; _stopBtn.Height = HeaderCtrlH; _stopBtn.Padding = new Thickness(0);
        _stopBtn.VerticalAlignment = VerticalAlignment.Center;
        // Same fix: FlatButtonTemplate so Background is honoured and the icon fill contrasts correctly.
        _stopBtn.Template = FlatButtonTemplate();
        _stopIcon = new System.Windows.Shapes.Path { Stretch = Stretch.Uniform,
            Data = Geometry.Parse("M3,3 H13 V13 H3 Z") };   // a stop square
        {
            var vb2 = new Viewbox();
            vb2.Width = 16; vb2.Height = 16;
            vb2.Stretch = Stretch.Uniform;
            vb2.HorizontalAlignment = HorizontalAlignment.Center;
            vb2.VerticalAlignment = VerticalAlignment.Center;
            vb2.Child = _stopIcon;
            _stopBtn.Content = vb2;
        }
        _stopBtn.Click += delegate
        {
            SendCommand(Cmd1("stop", true));
            // FIX B: immediate optimistic feedback -- don't wait for the ~700ms sweep to show
            // anything changed. Flip the button into its "stopping" state and dim every
            // non-terminal card NOW; RefreshStoppingState (called each OnTick) clears this the
            // moment the sweep confirms the run actually stopped (or a different run starts).
            _stopping = true;
            _stopStartedMarker = _lastRoot != null ? S(_lastRoot, "started") : "";
            PaintStop();
            ForceRender();
            // No live fleet to consume the stop (status went stale, e.g. a run was killed and its
            // last status froze with workers still shown as "running") -> clear every card NOW so the
            // button is never a no-op. Mirrors the per-card release stale path (search ArchiveAndHide).
            // The age test alone also matches a wedged-but-alive runner, whose unfinished workers
            // would be hidden and whose sidecars would be finalized under it. Require the runner
            // PROCESS to be gone; a live one consumes the stop command above when it recovers.
            if (_lastRoot != null
                && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                && (NowUnix() - Dbl(_lastRoot, "updated")) > 8
                && !RunnerProcessAlive())
            {
                ArchiveAllStale();
                // ArchiveAllStale only edits the LOCAL _history/_hiddenKeys lists (history.json /
                // cockpit_hidden.json) -- it never touches status.json or fleet_run_active.json, so
                // a fresh cockpit launch (or any other reader) would still see the phantom
                // running=true. Finalize the actual sidecars too. See relay/fleet_reaper.py.
                ReapStaleFleetRun();
            }
        };
        group.Children.Add(_stopBtn);

        PaintPause();
        PaintStop();
        return group;
    }
    // Icon buttons (spec): pause shows two bars; while paused it shows a play triangle (resume).
    // Quiet neutral chrome — no orange fill. The intent is carried by the icon + tooltip, not color.
    void PaintPause()
    {
        if (_pauseBtn == null) return;
        if (_pauseIcon != null)
        {
            _pauseIcon.Data = Geometry.Parse(_paused ? "M4,2 L13,8 L4,14 Z"          // play (resume)
                                                      : "M3,2 H6 V14 H3 Z M10,2 H13 V14 H10 Z"); // pause bars
            _pauseIcon.Fill = _paused ? Theme.Br(Theme.Accent(_dark)) : Fg;
        }
        _pauseBtn.ToolTip = _paused
            ? (_lang == 0 ? "再開（凍結を解除して続行）" : "Resume (unfreeze and continue)")
            : (_lang == 0 ? "新規ターン/タブを止めて凍結（再開で続行・状態は保持）" : "Freeze: no new turns/tabs (resume continues; state kept)");
        _pauseBtn.Background = BtnBg; _pauseBtn.Foreground = Fg; _pauseBtn.BorderBrush = Border;
        if (_stopIcon != null && !_stopping) _stopIcon.Fill = Fg;
    }
    // FIX B: paints the Stop button's optimistic "stopping" state (accent-colored icon + tooltip/
    // automation-name swap), mirroring PaintPause's icon-swap pattern for Pause/Resume. Called from
    // the same spots PaintPause is (theme/lang refresh + the click handler) so it never goes stale.
    void PaintStop()
    {
        if (_stopBtn == null) return;
        if (_stopIcon != null) _stopIcon.Fill = _stopping ? Theme.Br(Theme.Accent(_dark)) : Fg;
        _stopBtn.ToolTip = _stopping
            ? T("stop_stopping")
            : (_lang == 0 ? "全ワーカーを停止して走行を終了" : "Cancel every worker and end the run");
        System.Windows.Automation.AutomationProperties.SetName(_stopBtn,
            _stopping ? T("stop_stopping") : (_lang == 0 ? "停止" : "Stop"));
    }
    // Per-tick: Pause is enabled only when a run is LIVE (something to pause). When no run is live we
    // also drop a stale "paused" state so the label can never sit on "Resume" over a dead/absent run.
    void RefreshPauseEnabled(Dictionary<string, object> root)
    {
        bool live = Liveness(root) == 1;
        if (_pauseBtn != null)
        {
            _pauseBtn.IsEnabled = live;
            _pauseBtn.Opacity = live ? 1.0 : 0.5;
            if (!live && _paused) { _paused = false; PaintPause(); }
        }
        // Codex P2 ④: Stop is a strong action; when no run is live it should read as inert, not
        // "still need to stop something?". Disable + dim it (re-enabled the moment a run goes live).
        if (_stopBtn != null)
        {
            _stopBtn.IsEnabled = live;
            _stopBtn.Opacity = live ? 1.0 : 0.5;
            _stopBtn.Cursor = live ? Cursors.Hand : Cursors.Arrow;
        }
    }

    // FIX B: resolve the optimistic "stopping" state set by the Stop click handler. Cleared (and a
    // re-render forced via the Sig() dependency below) once the sweep confirms the run is no longer
    // LIVE -- i.e. actually stopped -- OR a different run has since started (a new `started` marker),
    // so the dimmed cards/button never stay stuck once the real signal arrives.
    void RefreshStoppingState(Dictionary<string, object> root)
    {
        if (!_stopping) return;
        bool live = Liveness(root) == 1;
        string startedNow = root != null ? S(root, "started") : "";
        bool newRun = !string.IsNullOrEmpty(_stopStartedMarker) && !string.IsNullOrEmpty(startedNow)
                      && startedNow != _stopStartedMarker;
        if (!live || newRun)
        {
            _stopping = false;
            PaintStop();
        }
    }

    // MaxTabsStepper() removed: the 開始(デフォルト) stepper now lives only in the settings panel
    // (BuildSettingsPanel builds it inline and assigns _maxMinus/_maxPlus/_maxValue).
    TextBlock _maxValue;

    // Autoscale toggle chip -- the SAME success/neutral treatment as PaintAutoRetryBtn and
    // PaintAutoArchiveBtn, which is the settings panel's rule for a status toggle.
    //
    // This one used the accent (orange) while the other two ONs beside it were green, so three
    // chips that mean the same thing -- this option is on -- read as three different kinds of
    // thing, and the odd one read as a warning. Accent fill is reserved for the primary Start
    // action; PaintAutoRetryBtn already said so and this chip was the exception to it.
    void PaintAutoToggle()
    {
        if (_autoToggle == null) return;
        _autoToggle.Content = T("autoscale") + ": " + (_autoscale ? "ON" : "OFF");
        if (_autoscale)
        {
            _autoToggle.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            _autoToggle.Foreground = Theme.Br(Theme.Success(_dark));
            _autoToggle.BorderBrush = Theme.Br(Theme.Success(_dark));
        }
        else
        {
            _autoToggle.Background = BtnBg;
            _autoToggle.Foreground = Muted;
            _autoToggle.BorderBrush = Border;
        }
    }

    // Ceiling stepper only matters under autoscale: grey/disable it when autoscale is off.
    void UpdateAutoEnabled()
    {
        bool on = _autoscale;
        if (_autoMinus != null) _autoMinus.IsEnabled = on;
        if (_autoPlus != null) _autoPlus.IsEnabled = on;
        double op = on ? 1.0 : 0.45;
        if (_autoMinus != null) _autoMinus.Opacity = op;
        if (_autoPlus != null) _autoPlus.Opacity = op;
        if (_autoValue != null) _autoValue.Opacity = op;
        if (_autoLbl != null) _autoLbl.Opacity = op;
    }

    void SetAutoMax(int v)
    {
        _autoMax = Math.Max(1, Math.Min(100, v));
        SaveKey("autoscale_max", _autoMax.ToString());
        if (_autoValue != null) _autoValue.Text = _autoMax.ToString();
        // live-apply only while autoscale is on (ceiling is meaningless otherwise).
        if (_autoscale) RequestSetAutoscaleIfLive();
    }

    // status.json freshness thresholds, mirrored from fleet_runner so the cockpit agrees with the
    // fleet's OWN watchdog on what "the driver is gone" means. A LIVE fleet legitimately freezes
    // status.json while a worker is blocked in a bounded acceptance eval -- up to EVAL_STALL_CEILING_S
    // when a worker is "verifying", otherwise up to the plain stall window. Treating that as dead would
    // be the WORSE bug (double-launch on Retry, dropped Steer), so we only call a run dead past these.
    const double DEAD_STALL_SECS = 150.0;        // == fleet_runner --stall-s default
    const double EVAL_STALL_CEILING_S = 1500.0;  // == relay_fleet.EVAL_STALL_CEILING_S

    // 0 = idle / no run, 1 = LIVE (fresh, or legitimately frozen inside a bounded eval),
    // 2 = DEAD (status claims running but has frozen past the stall window with no eval in flight,
    //     i.e. the fleet_runner process is gone). Reads status.json ONCE. Pure given the file.
    int Liveness(Dictionary<string, object> st)
    {
        if (st == null) return 0;
        bool running = !st.ContainsKey("running") || Convert.ToBoolean(st["running"]);
        bool idle = st.ContainsKey("idle") && Convert.ToBoolean(st["idle"]);
        if (!running || idle) return 0;
        double age = NowUnix() - Dbl(st, "updated");
        if (age <= DEAD_STALL_SECS) return 1;             // fresh enough -> live
        // frozen past the stall window: still LIVE iff a worker is in a bounded eval (mirrors
        // fleet_runner._watchdog_should_reset), bounded by the eval ceiling as the failsafe.
        bool evalInFlight = false;
        if (st.ContainsKey("workers") && st["workers"] is object[])
            foreach (object o in (object[])st["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                if (NowUnix() < Dbl(w, "eval_busy_until")) { evalInFlight = true; break; }
                if (S(w, "status") == "verifying") { evalInFlight = true; break; }
            }
        if (evalInFlight) return age > EVAL_STALL_CEILING_S ? 2 : 1;
        return 2;                                          // frozen, nothing in eval -> driver gone
    }

    // true iff a run is currently LIVE: running, not idle, AND its status is fresh enough that the
    // fleet_runner is provably still consuming commands.d/ (so steer/retry/pause actually land).
    bool RunIsLive()
    {
        try { return Liveness(ReadStatus()) == 1; }
        catch (Exception) { return false; }
    }
    // true iff a run claims to be running but its driver has died (frozen status). Used to switch
    // controls to their honest local fallback instead of writing commands nobody will ever read.
    bool RunIsDead()
    {
        try { return Liveness(ReadStatus()) == 2; }
        catch (Exception) { return false; }
    }
    void RequestSetAutoscaleIfLive()
    {
        if (RunIsLive()) RequestSetAutoscale(true, Math.Min(_maxtabs, _autoMax), _autoMax);
    }
    void RequestSetMaxtabsIfLive()
    {
        if (RunIsLive()) RequestSetMaxtabs(_maxtabs);
    }
    void SetMaxTabs(int v)
    {
        _maxtabs = Math.Max(1, Math.Min(100, v));
        SaveKey("maxtabs", _maxtabs.ToString());
        if (_maxValue != null) _maxValue.Text = _maxtabs.ToString();
        // FIX D: the same stepper gesture must behave the same whether autoscale is ON or OFF.
        // RequestSetMaxtabs writes a command file via the SAME live-apply mechanism
        // RequestSetAutoscale (and the old "Apply now" banner button) already used -- there is no
        // technical reason the non-autoscale path needs a separate negotiation banner, so both
        // paths now apply immediately and show the identical lightweight auto-dismissing toast
        // (ShowScaleToast -- the same self-fading overlay already used for UI-zoom feedback).
        bool running = RunIsLive();
        if (running)
        {
            if (_autoscale) RequestSetAutoscale(true, Math.Min(_maxtabs, _autoMax), _autoMax);
            else RequestSetMaxtabs(_maxtabs);
            if (_mtBanner != null) _mtBanner.Visibility = Visibility.Collapsed;
            ShowScaleToast(T("maxtabs_toast_prefix") + " " + _maxtabs);
        }
        else if (_mtBanner != null) _mtBanner.Visibility = Visibility.Collapsed;
    }

    Border _mtBanner;
    TextBlock _mtBannerLbl;

    // B: changing max-tabs during a live run -> apply now or next run.
    UIElement BuildMtBanner()
    {
        _mtBanner = new Border();
        _mtBanner.Visibility = Visibility.Collapsed;
        _mtBanner.CornerRadius = new CornerRadius(Theme.RadPopover);
        _mtBanner.BorderThickness = new Thickness(1);
        _mtBanner.Padding = new Thickness(16, 8, 12, 8);
        _mtBanner.Margin = new Thickness(24, 0, 16, 6);
        DockPanel.SetDock(_mtBanner, Dock.Top);
        var dp = new DockPanel();
        var btns = new StackPanel(); btns.Orientation = Orientation.Horizontal;
        btns.HorizontalAlignment = HorizontalAlignment.Right;
        DockPanel.SetDock(btns, Dock.Right);
        _mtApplyNow = new Button();
        _mtApplyNow.Cursor = Cursors.Hand; _mtApplyNow.BorderThickness = new Thickness(0);
        _mtApplyNow.Padding = new Thickness(12, 4, 12, 4); _mtApplyNow.FontWeight = FontWeights.SemiBold;
        _mtApplyNow.Click += delegate
        {
            if (_autoscale) RequestSetAutoscale(true, Math.Min(_maxtabs, _autoMax), _autoMax);
            else RequestSetMaxtabs(_maxtabs);
            _mtBanner.Visibility = Visibility.Collapsed;
        };
        btns.Children.Add(_mtApplyNow);
        _mtLater = new Button();
        _mtLater.Cursor = Cursors.Hand; _mtLater.BorderThickness = new Thickness(1);
        _mtLater.Padding = new Thickness(12, 4, 12, 4); _mtLater.Margin = new Thickness(8, 0, 0, 0);
        _mtLater.Click += delegate { _mtBanner.Visibility = Visibility.Collapsed; };
        btns.Children.Add(_mtLater);
        dp.Children.Add(btns);
        _mtBannerLbl = new TextBlock();
        _mtBannerLbl.VerticalAlignment = VerticalAlignment.Center; _mtBannerLbl.FontSize = 13;
        dp.Children.Add(_mtBannerLbl);
        _mtBanner.Child = dp;
        return _mtBanner;
    }
    Button _mtApplyNow, _mtLater;

    // ── TASK 1+2: capacity-wait banner + 強制開始 (force-start) ──────────────────────
    // The runner's admission gate holds new workers in status=="pending"/pill=="待機列" whenever
    // C: free drops below the reserved disk floor (or free RAM is too tight) -- but nothing told the
    // user WHY a task just sits queued. This banner surfaces that condition reactively each OnTick
    // and offers 強制開始, which disables the disk gate live via {"set_disk_floor_gb":0.0}
    // (consumed by fleet_runner._drain_commands, fleet_runner.py ~L561-563). It's reversible: we
    // capture the floor we zeroed and offer 床を戻す to write it back.
    Border _capBanner;
    TextBlock _capBannerLbl;
    Button _capForceBtn, _capRestoreBtn;
    bool _diskFloorForced = false;      // true once the user hit 強制開始 (gate disabled live)
    double _diskFloorPrev = 0.0;        // the floor we zeroed, so 床を戻す can restore it

    // RAM admission headroom (mirrors relay_fleet.auto_concurrency headroom_mb=2048): when free
    // physical RAM is around/under this the runner can't open another tab. We additionally surface
    // RAM in the banner when avail_mb is conspicuously low, even if disk is fine.
    const int RAM_FLOOR_MB = 2048;

    UIElement BuildCapBanner()
    {
        _capBanner = new Border();
        _capBanner.Visibility = Visibility.Collapsed;
        _capBanner.CornerRadius = new CornerRadius(Theme.RadPopover);
        _capBanner.BorderThickness = new Thickness(1);
        _capBanner.Padding = new Thickness(16, 8, 12, 8);
        _capBanner.Margin = new Thickness(24, 0, 16, 6);
        DockPanel.SetDock(_capBanner, Dock.Top);
        var dp = new DockPanel();
        var btns = new StackPanel(); btns.Orientation = Orientation.Horizontal;
        btns.HorizontalAlignment = HorizontalAlignment.Right;
        DockPanel.SetDock(btns, Dock.Right);
        _capForceBtn = new Button();
        _capForceBtn.Cursor = Cursors.Hand; _capForceBtn.BorderThickness = new Thickness(0);
        _capForceBtn.Padding = new Thickness(12, 4, 12, 4); _capForceBtn.FontWeight = FontWeights.SemiBold;
        _capForceBtn.Template = FlatButtonTemplate();
        _capForceBtn.Click += delegate { ForceStart(); };
        btns.Children.Add(_capForceBtn);
        _capRestoreBtn = new Button();
        _capRestoreBtn.Cursor = Cursors.Hand; _capRestoreBtn.BorderThickness = new Thickness(1);
        _capRestoreBtn.Padding = new Thickness(12, 4, 12, 4); _capRestoreBtn.Margin = new Thickness(8, 0, 0, 0);
        _capRestoreBtn.Template = FlatButtonTemplate();
        _capRestoreBtn.Visibility = Visibility.Collapsed;
        _capRestoreBtn.Click += delegate { RestoreFloor(); };
        btns.Children.Add(_capRestoreBtn);
        dp.Children.Add(btns);
        _capBannerLbl = new TextBlock();
        _capBannerLbl.VerticalAlignment = VerticalAlignment.Center; _capBannerLbl.FontSize = 13;
        _capBannerLbl.TextWrapping = TextWrapping.Wrap;
        dp.Children.Add(_capBannerLbl);
        _capBanner.Child = dp;
        return _capBanner;
    }

    // ── command_rejections banner ─────────────────────────────────────────────────────────
    // status.json's `command_rejections` (relay/fleet_runner.py, e822fb6): every fleet command
    // (from this cockpit, CopilotChat, or a stale/foreign writer) that validate_command refused
    // is recorded here, newest last, up to MAX_REJECTIONS_KEPT=20. Before this the only place a
    // refusal was visible was the fleet's own console log and a landing receipt nobody watching
    // the cockpit would think to open -- an operator could send a bad set_disk_floor_gb or
    // add_goal and see nothing happen, with no clue why. Plain, one line, not a modal: this is
    // information about something that already happened and needs no decision from the operator.
    Border _rejBanner;
    TextBlock _rejBannerLbl;

    UIElement BuildRejectionBanner()
    {
        _rejBanner = new Border();
        _rejBanner.Visibility = Visibility.Collapsed;
        _rejBanner.CornerRadius = new CornerRadius(Theme.RadPopover);
        _rejBanner.BorderThickness = new Thickness(1);
        _rejBanner.Padding = new Thickness(16, 8, 12, 8);
        _rejBanner.Margin = new Thickness(24, 0, 16, 6);
        DockPanel.SetDock(_rejBanner, Dock.Top);
        _rejBannerLbl = new TextBlock();
        _rejBannerLbl.VerticalAlignment = VerticalAlignment.Center; _rejBannerLbl.FontSize = 13;
        _rejBannerLbl.TextWrapping = TextWrapping.Wrap;
        _rejBanner.Child = _rejBannerLbl;
        return _rejBanner;
    }

    // Reactively show the NEWEST rejection each tick (rows are append-only, newest last -- see
    // record_command_rejection). Hides when the array is empty or unreadable. `keys`/`errors`
    // are exactly what relay/fleet_runner.py's record_command_rejection recorded: the refused
    // command's KEYS and validate_command's reasons, deliberately never the values a steer or a
    // goal carried.
    void UpdateRejectionBanner(Dictionary<string, object> root)
    {
        if (_rejBanner == null) return;
        object ro;
        object[] rows = (root != null && root.TryGetValue("command_rejections", out ro) && ro is object[])
                        ? (object[])ro : null;
        Dictionary<string, object> last = null;
        if (rows != null)
            for (int i = rows.Length - 1; i >= 0 && last == null; i--)
                last = rows[i] as Dictionary<string, object>;
        if (last == null)
        {
            _rejBanner.Visibility = Visibility.Collapsed;
            return;
        }
        string keys = "";
        object ko;
        if (last.TryGetValue("keys", out ko) && ko is object[])
            keys = string.Join(",", Array.ConvertAll((object[])ko, x => x == null ? "" : x.ToString()));
        string errs = "";
        object eo;
        if (last.TryGetValue("errors", out eo) && eo is object[])
            errs = string.Join("; ", Array.ConvertAll((object[])eo, x => x == null ? "" : x.ToString()));
        string when = "";
        if (last.ContainsKey("ts"))
        {
            try { when = AgeMinutesText((NowUnix() - Convert.ToDouble(last["ts"])) / 60.0); }
            catch (Exception) { }
        }
        string msg = L(
            "コマンドが拒否されました" + (when.Length == 0 ? "" : "（" + when + "）") + "：" +
            (keys.Length == 0 ? "(キーなし)" : keys) + (errs.Length == 0 ? "" : " — " + errs),
            "Command rejected" + (when.Length == 0 ? "" : " (" + when + ")") + ": " +
            (keys.Length == 0 ? "(no keys)" : keys) + (errs.Length == 0 ? "" : " — " + errs));
        _rejBannerLbl.Text = msg;
        _rejBanner.Visibility = Visibility.Visible;
    }

    // Reactively show/hide the capacity-wait banner each tick. Condition: a LIVE run with at least
    // one worker held at "pending"/"待機列" AND (disk below floor OR RAM conspicuously low). Once
    // the gate clears (or the run ends) the banner hides itself -- it never sticks.
    void UpdateCapBanner(Dictionary<string, object> root)
    {
        if (_capBanner == null) return;

        // The 床OFF (force-running) indicator is independent of the queue: while the user has the
        // gate disabled, keep showing it (with 床を戻す) so the override is never silent.
        if (_diskFloorForced)
        {
            _capBanner.Visibility = Visibility.Visible;
            _capBannerLbl.Text = T("floor_off");
            _capForceBtn.Visibility = Visibility.Collapsed;
            _capRestoreBtn.Visibility = Visibility.Visible;
            _capRestoreBtn.Content = T("floor_restore");
            return;
        }

        bool running = root != null && (!root.ContainsKey("running") || Convert.ToBoolean(root["running"]));
        bool pendingHeld = false;
        if (running && root.ContainsKey("workers") && root["workers"] is object[])
            foreach (object o in (object[])root["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w != null && S(w, "status") == "pending") { pendingHeld = true; break; }
            }

        double freeDisk = Dbl(root ?? new Dictionary<string, object>(), "free_disk_gb");
        double floor = Dbl(root ?? new Dictionary<string, object>(), "disk_floor_gb");
        int availMb = I(root ?? new Dictionary<string, object>(), "avail_mb");
        bool diskGated = floor > 0 && freeDisk > 0 && freeDisk < floor;
        // gate on the live status' ram_floor_mb if present, else the cockpit's configurable _ramFloor
        // (no longer the hardcoded RAM_FLOOR_MB const).
        double ramFloorNow = Dbl(root ?? new Dictionary<string, object>(), "ram_floor_mb");
        if (ramFloorNow <= 0) ramFloorNow = _ramFloor;
        bool ramGated = availMb > 0 && ramFloorNow > 0 && availMb < ramFloorNow;

        if (running && pendingHeld && (diskGated || ramGated))
        {
            _capForceBtn.Visibility = Visibility.Visible;
            _capForceBtn.Content = T("force_start");
            _capRestoreBtn.Visibility = Visibility.Collapsed;
            string msg;
            if (diskGated)
            {
                int need = (int)Math.Ceiling(floor - freeDisk);
                msg = _lang == 0
                    ? "ディスクの空きが少ないため、新しいタブを開かずに待機しています（空き " + freeDisk + "GB / 確保 " + floor + "GB）。"
                      + need + "GB 空けると自動で再開します。"
                    : "Disk space is low, so new tabs are paused (free " + freeDisk + "GB / floor " + floor + "GB). "
                      + "Freeing " + need + "GB resumes automatically.";
                if (ramGated)
                    msg += _lang == 0 ? "（空きRAMも " + availMb + "MB と少なめ）"
                                      : " (free RAM is also low at " + availMb + "MB)";
            }
            else
            {
                msg = _lang == 0
                    ? "RAMが少ないため、新しいタブを開かずに待機しています（空き " + availMb + "MB）。"
                    : "RAM is low, so new tabs are paused (free " + availMb + "MB).";
            }
            _capBannerLbl.Text = msg;
            _capBanner.Visibility = Visibility.Visible;
        }
        else
        {
            _capBanner.Visibility = Visibility.Collapsed;
        }
    }

    // 強制開始: disable the disk gate live. Capture the current floor first so 床を戻す can restore
    // it. Writes {"set_disk_floor_gb":0.0} via the SAME one-file-per-command writer Pause uses, so a
    // queued close/steer/set_maxtabs isn't clobbered. Consumed by fleet_runner.py ~L561.
    void ForceStart()
    {
        if (!RunIsLive()) return;     // nothing alive to consume the command
        var root = ReadStatus();
        _diskFloorPrev = Dbl(root ?? new Dictionary<string, object>(), "disk_floor_gb");
        SendCommand(Cmd1("set_disk_floor_gb", 0.0));
        _diskFloorForced = true;
        UpdateCapBanner(root);
    }

    // 床を戻す: write the previously-captured floor back, re-arming the disk gate.
    void RestoreFloor()
    {
        SendCommand(Cmd1("set_disk_floor_gb", _diskFloorPrev));
        _diskFloorForced = false;
        UpdateCapBanner(ReadStatus());
    }
}
