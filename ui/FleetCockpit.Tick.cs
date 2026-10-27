// FleetCockpit.Tick.cs -- the refresh tick: OnTick, auto-archive, auto-retry scan, ReadStatus.
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

    // ── poll loop ─────────────────────────────────────────────────────────────────
    // TOP-LEVEL GUARD. OnTick is a DispatcherTimer handler, so an exception that escapes it is
    // unhandled on the UI thread and TERMINATES the process -- which is exactly how the cockpit
    // died three times on 2026-10-27 (a NullReferenceException deep in the first RenderCards,
    // S <- RowSig <- MkRow <- AppendHistoryRows). The real poll work now lives in OnTickCore;
    // this wrapper is the one place that catches anything it throws so a bad tick SKIPS rather
    // than kills, shows the cause on screen, and records it once. The next tick runs normally.
    void OnTick(object sender, EventArgs e)
    {
        try { OnTickCore(sender, e); }
        catch (Exception ex) { OnTickFailed(ex); }
    }

    void OnTickCore(object sender, EventArgs e)
    {
        // follow external theme/lang/maxtabs edits (e.g. the chat toggled the theme)
        try
        {
            if (File.Exists(SettingsFile))
            {
                long m = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
                if (m != _settingsMtime)
                {
                    bool d0 = _dark; int l0 = _lang; double s0 = _uiScale;
                    bool a0 = _uiAuto; double t0 = _scaleTarget;
                    LoadSettings();
                    // The fan-out / effort-policy controls live in the gear popup, which may not
                    // exist yet (every Paint* is null-safe) and is rebuilt on each open. Repaint
                    // whatever is there so an external settings.txt edit shows without reopening.
                    // Paint* only assigns when different, so SelectionChanged does not re-fire.
                    PaintEffort();
                    PaintEffortPolicy();
                    PaintFanout();
                    if (d0 != _dark) { ApplyThemeBrushes(); PaintChrome(); _lastSig = ""; }
                    else if (l0 != _lang) { RebuildChrome(); }
                    // External ui_scale edit (e.g. the chat app zoomed / switched to auto): apply it live
                    // so both apps stay in lock-step. In AUTO the effective scale is per-monitor, so we
                    // recompute for THIS window's monitor (not the other app's) — silently. In MANUAL we
                    // push the shared number and flash the %. RebuildChrome above (if lang changed) already
                    // re-anchored the transform on the new root.
                    if (_uiAuto)
                    {
                        // Recompute if we just entered auto, or the target changed, so the transform tracks.
                        if (!a0 || Math.Abs(t0 - _scaleTarget) > 0.001 || Math.Abs(s0 - EffectiveAutoScale(CurrentMonitorScale())) > 0.001)
                            ApplyAutoScale(false);
                        RefreshUiScaleControls();
                    }
                    else if (a0 || Math.Abs(s0 - _uiScale) > 0.001)
                    {
                        // Other app set a manual number (or switched auto->manual): reflect + toast.
                        _uiScale = Math.Max(0.8, Math.Min(2.0, _uiScale));
                        PushScaleToTransform(); RefreshUiScaleControls(); ShowScaleToast();
                    }
                }
            }
        }
        catch (Exception) { }

        Dictionary<string, object> root = ReadStatus();
        RefreshSubmitted(root);             // the "submitted, not picked up yet" group (top of the list)
        RefreshPauseEnabled(root);          // Pause is only meaningful for a live run; grey it out otherwise
        RefreshStoppingState(root);         // FIX B: resolve the optimistic "stopping" state once the sweep confirms it
        UpdateGateBanner(root);             // Bucket C TASK 2: show pending approval gates (blocks worker until answered)
        UpdateCapBanner(root);              // TASK 1: surface the admission-gate wait reactively each tick
        UpdateRejectionBanner(root);        // show the newest refused fleet command (SEC-08, e822fb6)
        // (the re-unlock receipt used to be turned into a panel note here; the harness now
        //  delivers unlocks itself and the receipt is read from status.json by status.py)
        bool idle = root == null || I(root, "total") == 0
                    || (root.ContainsKey("idle") && Convert.ToBoolean(root["idle"]));
        if (idle)
        {
            _header.Text = "Fleet";
            _sub.Text = "";                // the empty-state block in the body carries the message now
            if (_subChips != null) _subChips.Children.Clear();   // Feature 2: no chips while idle either
            UpdateWorkerChip(0, 0, false); // configuration lives in Settings while idle
            // A2-2: collapse spine and idle composer when no run
            RefreshSpine(root, true);
            PaintComposerMode(false);
            // Idle: only history (or the empty state) is shown — no live board to filter, so the
            // pinned filter bar is hidden here just as the toolbar row was never emitted when idle.
            if (_pinnedToolbarHost != null)
            {
                _pinnedToolbarHost.Visibility = Visibility.Collapsed;
                _pinnedToolbarHost.Child = null;
                _pinnedToolbarSig = "";
            }
            // P2 RESUME affordance: only meaningful when NO run is live (this idle branch). Compute the
            // unfinished count from the last run's durable ledger; hide when 0.
            int resumeN = UnfinishedResumeCount();
            string resumeSig = resumeN > 0 ? ResumeStateSignature() : "";
            bool resumeDismissed = resumeN > 0 && ResumeStateDismissed(resumeSig);
            string isig = "IDLE" + _history.Count + (_dark ? "D" : "L") + _lang + "|q" + (_histQuery ?? "")
                          + "|r" + resumeN + "|rs" + resumeSig + "|rd" + (resumeDismissed ? "1" : "0")
                          + "|s" + _submittedSig;
            if (_lastSig != isig)
            {
                _lastRoot = null;
                var rows = new List<object>();
                // FIRST, in the idle state too: a submission made while nothing runs (StartFleet
                // before the run's first snapshot, fleet_submit, a for_fleet hand-off) is on top.
                AppendSubmittedRows(rows);
                int beforeRest = rows.Count;
                if (resumeN > 0 && !resumeDismissed)
                {
                    var rd = new Dictionary<string, object>();
                    rd["n"] = resumeN; rd["signature"] = resumeSig;
                    rows.Add(MkRow(8, null, rd));   // resume affordance (idle + N>0 only)
                }
                AppendHistoryRows(rows, null);   // idle: no live run on board -> show all history
                // empty state when nothing but (possibly) submitted rows is shown
                if (rows.Count == beforeRest) rows.Add(MkRow(5, null, null));
                SetRows(rows);
                _lastSig = isig;
            }
            return;
        }
        UpdateHeader(root);                 // live elapsed every tick
        bool runningNow = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
        // Snapshot exposes actual browser tabs and the current live concurrency cap. This is
        // operational status, not another place to edit those settings.
        {
            int openTabs = I(root, "open_tabs");
            int liveCap = I(root, "max_concurrent");
            UpdateWorkerChip(openTabs, liveCap, runningNow);
            if (runningNow) ApplyRosterToChip(root);   // who works on what: the chip's tooltip
        }
        // only archive while the run is LIVE -- otherwise the finished run's final
        // snapshot would re-add cleared tasks every tick (Clear would never stick).
        //
        // AND ONCE MORE WHEN IT STOPS, WHICH IS THE WHOLE BUG. `running` is false as soon as
        // every worker is terminal, so the sweep in which the LAST worker finishes is the
        // sweep that fails this test -- that worker was never terminal on a tick where this
        // line ran, and it never reaches History.
        //
        // MEASURED 2026-08-28 over the eight most recent runs: seven lost at least one
        // worker, the missing one was the highest-numbered in five, and a ONE-GOAL run
        // archived nothing at all -- 0 of 1, because its only worker finished on the sweep
        // that flipped running to false.
        //
        // Fired once per run, keyed on `started`, so the objection above still holds: a
        // finished run cannot re-add cleared cards every tick, because it archives on one
        // tick only. Independent of the auto-archive setting and of which cards are on
        // screen -- MaybeAutoArchive walks _toolbarShown, so with a tab filter active it
        // archives only what the filter left visible, and this walks the snapshot.
        if (runningNow) ArchiveTerminal(root);
        else ArchiveRunTailOnce(root);
        // opt-in auto-retry runs every tick (before the sig short-circuit) so it catches a
        // stopped goal even when nothing else changed. Bounded by _autoRetryMax per goal text.
        if (runningNow && _autoRetry) AutoRetryScan(root);
        // P2 (c) AUTO-ARCHIVE: when the RUN has finished (not running AND every worker terminal),
        // move its completed cards to History -- reusing the exact "すべて履歴へ" code path
        // (ArchiveAllTerminal). Fires ONCE per run, keyed on `started`, and only when enabled.
        if (_autoArchive && !runningNow) MaybeAutoArchive(root);
        // A2-2: spine refresh runs every tick (keyed off its own sig, cheap when unchanged).
        // _toolbarAll is populated by the last RenderCards/BuildRows; for the first tick it may
        // be empty, but RefreshSpine re-reads workers from root directly so that's fine.
        // PaintComposerMode: show "steer" surface while run is live.
        RefreshSpine(root, false);
        PaintComposerMode(runningNow);
        string sig = Sig(root) + "|s" + _submittedSig;
        if (sig == _lastSig) return;
        _lastSig = sig;
        RenderCards(root);
    }

    // P2 (c): if the run has finished and every on-board worker is terminal, archive them ALL to
    // History via the same path the "すべて履歴へ" button uses. Guarded to fire once per run by
    // the run's `started` identity (a new run resets it) so a finished snapshot re-read each tick
    // doesn't re-archive (and Clear can still stick).
    void MaybeAutoArchive(Dictionary<string, object> root)
    {
        if (root == null) return;
        string started = S(root, "started");
        if (string.IsNullOrEmpty(started)) return;
        if (_archivedRunStarted == started) return;            // already handled this run

        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        var wArr = (object[])wo;
        if (wArr.Length == 0) return;
        foreach (object o in wArr)
        {
            var w = o as Dictionary<string, object>;
            if (w == null) continue;
            if (IsInterruptedWorker(w)) continue;              // resumable: never archived, never blocks
            if (!IsTerminalWorker(w)) return;                  // not fully finished yet -> wait
        }
        // _toolbarShown is populated by the last RenderCards; on the finished tick it holds this
        // run's terminal workers. If it hasn't been built yet (empty), defer to a later tick rather
        // than marking the run handled with nothing archived.
        if (_toolbarShown == null || _toolbarShown.Count == 0) return;
        _archivedRunStarted = started;                         // mark BEFORE archiving (idempotent)
        ArchiveAllTerminal();                                   // reuse the exact bulk-archive path
    }

    // Opt-in, CAPPED auto-retry. For each STOPPED non-DONE goal, re-queue it at most
    // _autoRetryMax times (counted by goal text). The cap is the safety rail: a goal that
    // keeps failing stops being re-queued once it hits the budget -- never an infinite loop.
    // Only acts while a run is live (add_goal is consumed by the running fleet).
    void AutoRetryScan(Dictionary<string, object> root)
    {
        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        foreach (object o in (object[])wo)
        {
            var w = o as Dictionary<string, object>;
            if (w == null) continue;
            if (!IsTerminalWorker(w)) continue;
            // THE SAME CLOSED SET PYTHON USES, not the one outcome that came to mind.
            //
            // This skipped only DONE, so every other outcome was retryable here -- including
            // FANOUT, which relay/outcomes.py classifies as done and explicitly NOT
            // retryable. relay/fleet_runner.py says why that matters in as many words: two
            // retry policies that can disagree is worse than either one alone.
            //
            // What the disagreement cost, measured 2026-08-28 over 100 coordinator logs: a
            // fan-out parent ends terminal with outcome FANOUT, so this re-queued it. The
            // duplicate carries the SAME GOAL TEXT and a higher worker index, and the fleet
            // keys delivered results by goal text -- so the duplicate overwrote the parent
            // that the merged answer had just been written onto. Of 24 runs that split, 22
            // delivered a duplicate instead of the merge. One run split 8 goals into 63
            // workers, merged all 8, and delivered none of them.
            //
            // So the fan-out was not broken. It ran, it split, it merged, and then this
            // line threw the answer away.
            if (!IsRetryableWorker(w)) continue;
            string goal = S(w, "goal");
            if (string.IsNullOrEmpty(goal)) continue;
            int n = 0;
            if (_autoRetryCount.ContainsKey(goal)) n = _autoRetryCount[goal];
            if (n >= _autoRetryMax) continue;          // budget spent -> never loop
            // ONE RE-QUEUE PER WORKER. The terminal worker stays terminal, so without this every
            // tick re-queued the same STUCK worker until the per-goal budget ran out (2026-10-01:
            // two extra copies on top of the runner's own retry).
            if (RetryAlreadyCovered(root, w)) continue;
            _autoRetriedWorkers.Add(RetryWorkerKey(w));
            _autoRetryCount[goal] = n + 1;             // count BEFORE re-queue (idempotent per tick)
            RetryGoal(w);
        }
    }

    Dictionary<string, object> ReadStatus()
    {
        try
        {
            if (!File.Exists(_statusPath)) return null;
            string text;
            using (var fs = new FileStream(_statusPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fs, Encoding.UTF8))
                text = sr.ReadToEnd();
            if (string.IsNullOrEmpty(text)) return null;
            return (Dictionary<string, object>)_js.DeserializeObject(text);
        }
        catch (Exception ex) { NoteStatusReadFailure(ex); return null; }
    }

    bool _statusReadFailLogged;

    // A SWALLOWED EXCEPTION WITH NO TRACE IS WHY THE BUG ABOVE TOOK TWENTY HOURS TO FIND.
    // The catch itself has to stay -- a cockpit that dies on a half-written status.json is
    // worse than one that skips a tick -- but "returns null on any failure" and "logs
    // nothing on any failure" are separate decisions, and only the first one was wanted.
    // Recording the FIRST failure per process is enough: the failure is structural, so the
    // thousandth line would say what the first one says, and an unbounded log on a
    // per-tick path is its own disk problem. Size is recorded because it is the field that
    // identifies this failure at a glance.
    void NoteStatusReadFailure(Exception ex)
    {
        if (_statusReadFailLogged) return;
        _statusReadFailLogged = true;
        try
        {
            long size = -1;
            try { size = new FileInfo(_statusPath).Length; } catch (Exception) { }
            string line = "{\"ts\":" + DateTimeOffset.UtcNow.ToUnixTimeSeconds()
                        + ",\"what\":\"status_read_failed\""
                        + ",\"bytes\":" + size
                        + ",\"type\":\"" + ex.GetType().Name + "\""
                        + ",\"detail\":\"" + (ex.Message ?? "").Replace("\\", "/").Replace("\"", "'") + "\"}";
            string path = Path.Combine(Path.GetDirectoryName(ResolvePath(null)), "ui_errors.jsonl");
            File.AppendAllText(path, line + Environment.NewLine, NoBomUtf8);
        }
        catch (Exception) { }   // a trace that can break the cockpit is worse than no trace
    }

    bool _tickFailLogged;

    // A TICK THAT THREW MUST NOT TAKE THE PROCESS WITH IT. Called from OnTick's top-level catch.
    // Shows the cause in the header area (so an operator sees WHY the board stopped updating
    // instead of a vanished window) and records the first failure per process to ui_errors.jsonl
    // for a trace. The screen text is refreshed on every failing tick; the file line is written
    // once (the failure is structural, and an unbounded log on a per-tick path is its own disk
    // problem). Everything here is itself guarded: the recovery path must never be the new
    // source of an unhandled exception.
    void OnTickFailed(Exception ex)
    {
        try
        {
            if (_header != null) _header.Text = "Fleet";
            if (_sub != null)
                _sub.Text = (_lang == 0 ? "更新中にエラー: " : "tick error: ")
                            + ex.GetType().Name + ": " + (ex.Message ?? "");
        }
        catch (Exception) { }
        try
        {
            if (_tickFailLogged) return;
            _tickFailLogged = true;
            string line = "{\"ts\":" + DateTimeOffset.UtcNow.ToUnixTimeSeconds()
                        + ",\"what\":\"tick_failed\""
                        + ",\"type\":\"" + ex.GetType().Name + "\""
                        + ",\"detail\":\"" + (ex.Message ?? "").Replace("\\", "/").Replace("\"", "'") + "\""
                        + ",\"at\":\"" + (ex.StackTrace ?? "").Replace("\\", "/").Replace("\"", "'").Replace("\r", " ").Replace("\n", " ") + "\"}";
            string path = Path.Combine(Path.GetDirectoryName(ResolvePath(null)), "ui_errors.jsonl");
            File.AppendAllText(path, line + Environment.NewLine, NoBomUtf8);
        }
        catch (Exception) { }   // a trace that can break the cockpit is worse than no trace
    }
}
