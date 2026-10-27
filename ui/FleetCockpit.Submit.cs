// FleetCockpit.Submit.cs -- submitted-task bookkeeping, StartFleet, durable/live add, spawn and resume of runs.
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

    // ── the TASK QUEUE, which this window never looked at ──────────────────────────────────
    //
    // This panel reads .fleet/status.json and nothing else -- the runner's snapshot of WORKERS.
    // A job submitted through fleet_submit lands in .fleet/tasks/pending/<id>.json and has no
    // worker until a coordinator starts, so for the whole gap between "on disk" and "a worker
    // exists" the queue was invisible here and EmptyState() said "タスクはまだありません" --
    // which was not slow, it was false. The operator watching this screen could not tell a
    // submission that landed from one that vanished, and the standing rule in this project is
    // that what cannot be confirmed in the GUI does not count as working. So a submitted job
    // could not be verified at all.
    //
    // A worker held at status "pending" ("待機列") is a DIFFERENT state -- the admission gate
    // holding a worker that exists -- and the two are shown apart on purpose.
    //
    // AND THEN IT WAS STILL INVISIBLE, because the queue was drawn only inside EmptyState():
    // with a run live or anything in history, a submitted job did not appear until a worker
    // existed for it. The queue is now one of three sources of a "submitted, not picked up
    // yet" group drawn at the TOP of the list in every state (ui/SubmittedTasks.cs says what
    // is merged and when an entry leaves). EmptyState and the published strip read the SAME
    // merged list, through ReadQueuedJobs(), so the three cannot disagree.
    readonly SubmittedTasks _submitted = new SubmittedTasks();
    // Replaced wholesale on the UI thread each tick and never mutated afterwards, so the health
    // poll's background thread can read it (PublishHealthStrip) without a lock.
    volatile List<SubmittedView> _submittedNow = new List<SubmittedView>();
    string _submittedSig = "";
    // The refused goals, read the same tick from .fleet/tasks/refused. Same publish discipline:
    // replaced whole on the UI thread, read without a lock. A refused card is NOT merged into the
    // submitted group -- it did not enter the queue -- but it is drawn in the same top area so a
    // turned-away request is seen at the machine and can be forced in by the person.
    volatile List<RefusedView> _refusedNow = new List<RefusedView>();

    static string TasksDir()
    {
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;          // ...\ui\
        return Path.GetFullPath(Path.Combine(exeDir, "..", ".fleet", "tasks"));
    }

    static string RefusedDir()
    {
        return Path.Combine(TasksDir(), "refused");
    }

    // What the "submitted" group shows right now: the merged list, newest first, as of the
    // last tick. The one read EmptyState, the rows and PublishHealthStrip all share.
    List<SubmittedView> ReadQueuedJobs()
    {
        List<SubmittedView> now = _submittedNow;
        return now ?? new List<SubmittedView>();
    }

    // The refused goals to draw this tick, newest first. A record already forced in is dropped
    // from the cards here: its job is now in the submitted group like any other, and a spent
    // button must not linger. Never throws.
    List<RefusedView> ReadRefusedCards()
    {
        var outp = new List<RefusedView>();
        List<RefusedView> now = _refusedNow;
        if (now == null) return outp;
        foreach (RefusedView v in now)
            if (v != null && !v.Forced) outp.Add(v);
        return outp;
    }

    // A "refused, not submitted" card: a goal fleet_submit turned away. The owner asked that the
    // system never silently drop a request -- so the reason is shown IN JAPANESE (for a near-
    // duplicate, naming the job it looked like and the measured overlap), the whole goal is on the
    // card, and a "強制投入" (force) button lets the PERSON overrule the refusal. The button
    // runs tools.fleet_intake --force-refused <id>, which is force_submit_refused -- an operator
    // entry deliberately kept out of the MCP tool list, so only a person at this machine reaches it.
    UIElement RefusedCard(RefusedView v)
    {
        bool ja = _lang == 0;
        var card = new Border {
            BorderThickness = new Thickness(1), BorderBrush = Border, Background = BtnBg,
            CornerRadius = new CornerRadius(Theme.RadCard),
            Padding = new Thickness(16, 8, 16, 8), Margin = new Thickness(8, 8, 8, 4) };
        if (v == null) { card.Child = new TextBlock { Text = "" }; return card; }
        var dp = new DockPanel { LastChildFill = true };
        // The action sits on the right, mirroring SubmittedRow's dismiss. A spent record never
        // reaches here (ReadRefusedCards drops Forced), so the button is always live.
        var force = new Button {
            Content = ja ? "強制投入" : "Force in", Cursor = Cursors.Hand, FontSize = 12,
            Foreground = Fg, Background = Brushes.Transparent,
            BorderBrush = Border, BorderThickness = new Thickness(1),
            Padding = new Thickness(12, 4, 12, 4), Margin = new Thickness(8, 0, 0, 0) };
        force.ToolTip = ja ? "判定を飛ばしてこの目標を投入します（利用者による強制）"
                           : "Queue this goal, skipping the admission checks (operator override)";
        string refusedId = v.Id;
        force.Click += delegate (object sender, RoutedEventArgs e)
        {
            e.Handled = true;
            ForceRefusedGoal(refusedId);
        };
        DockPanel.SetDock(force, Dock.Right); dp.Children.Add(force);
        var stack = new StackPanel();
        stack.Children.Add(new TextBlock {
            Text = SubmittedTasks.RefusedReasonText(v, ja),
            Foreground = Muted, FontSize = 11.5, FontWeight = FontWeights.SemiBold,
            TextWrapping = TextWrapping.Wrap });
        stack.Children.Add(new TextBlock {
            Text = OneLine(v.Goal, 140),
            Foreground = Fg, FontSize = 13, Margin = new Thickness(0, 2, 0, 0),
            TextTrimming = TextTrimming.CharacterEllipsis, ToolTip = v.Goal });
        dp.Children.Add(stack);
        card.Child = dp;
        return card;
    }

    // Run the operator-only force path for one refused record, off the MCP surface: launch
    // tools.fleet_intake --force-refused <id> (force_submit_refused), then refresh so the card --
    // whose record is now stamped forced -- drops on the next read. Never throws into the click.
    void ForceRefusedGoal(string refusedId)
    {
        if (string.IsNullOrEmpty(refusedId)) return;
        try
        {
            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m tools.fleet_intake --force-refused \"" + refusedId + "\" --source cockpit";
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            System.Diagnostics.Process.Start(psi);
        }
        catch (Exception) { }
        // The pending job and the stamped record land on disk; the next tick reads both. Nudge a
        // render so the forced card leaves promptly rather than waiting for the status poll.
        ForceRender();
    }

    // Once per tick: read the three on-disk sources and merge them with this window's own
    // submissions. NEVER THROWS INTO THE TICK -- SubmittedTasks.ReadFiles swallows a torn or
    // half-written file, and anything else here leaves the previous list on screen.
    //
    // WHERE IT LOOKS, named here rather than inside SubmittedTasks so the panel's published
    // strip (queue_dir) and this read cannot point at different places:
    //   <state dir>/commands.d   add_goal commands no run has consumed yet
    //   <tasks>/"pending"        fleet_submit's queue, not yet routed
    //   <tasks>/"for_fleet"      routed, waiting for a fleet to start
    void RefreshSubmitted(Dictionary<string, object> root)
    {
        try
        {
            string tasks = TasksDir();
            List<SubmittedFile> files = SubmittedTasks.ReadFiles(
                Path.Combine(_fleetDir, "commands.d"),
                Path.Combine(tasks, "pending"),
                Path.Combine(tasks, "for_fleet"));
            List<SubmittedView> views = _submitted.Refresh(files, StartedOf(root),
                                                           WorkersOf(root), HistoryWorkers(), NowUnix());
            _submittedNow = views;
            _submittedSig = SubmittedTasks.Signature(views, _lang == 0);
            // Read the refusals the same tick, from the same tasks dir, so the submitted group
            // and its refused cards cannot point at different places. ReadRefused never throws.
            _refusedNow = SubmittedTasks.ReadRefused(RefusedDir());
        }
        catch (Exception) { }
    }

    static string StartedOf(Dictionary<string, object> root)
    {
        return root == null ? "" : S(root, "started");
    }

    static List<Dictionary<string, object>> WorkersOf(Dictionary<string, object> root)
    {
        var l = new List<Dictionary<string, object>>();
        object wo;
        if (root != null && root.TryGetValue("workers", out wo) && wo is object[])
            foreach (object o in (object[])wo)
            {
                var d = o as Dictionary<string, object>;
                if (d != null) l.Add(d);
            }
        return l;
    }

    List<Dictionary<string, object>> HistoryWorkers()
    {
        var l = new List<Dictionary<string, object>>();
        if (_history == null) return l;
        foreach (object o in _history)
        {
            var d = o as Dictionary<string, object>;
            if (d != null) l.Add(d);
        }
        return l;
    }

    // THE INSTANT THIS WINDOW SUBMITS: put each goal in the "submitted" group before any file
    // is read back, then re-render on the next dispatcher turn. BeginInvoke, not a direct
    // ForceRender: AutoRetryScan calls RetryGoal from inside OnTick, and a nested OnTick there
    // would re-enter the scan. The status.json read here is what a retry's goal is measured
    // against -- the worker being retried is already on the board with this very goal text,
    // and must not count as the new submission's worker (see SubmittedTasks.AddLocal).
    // Snapshot what existed BEFORE a submission leaves this process. A worker can appear in the
    // few milliseconds between SendCommand/Process.Start and the optimistic UI row. If we sample
    // after the handoff, that brand-new worker looks "preexisting" and the submitted row can
    // remain `taken` forever even though the fleet already created its worker.
    sealed class SubmissionBaseline
    {
        public string Started;
        public List<Dictionary<string, object>> Workers;
        public List<Dictionary<string, object>> History;
        public double CapturedUnix;
    }

    SubmissionBaseline CaptureSubmissionBaseline()
    {
        try
        {
            Dictionary<string, object> root = ReadStatus();
            return new SubmissionBaseline {
                Started = StartedOf(root),
                Workers = WorkersOf(root),
                History = HistoryWorkers(),
                CapturedUnix = NowUnix() };
        }
        catch (Exception)
        {
            return new SubmissionBaseline {
                Started = "", Workers = new List<Dictionary<string, object>>(),
                History = new List<Dictionary<string, object>>(), CapturedUnix = NowUnix() };
        }
    }

    // ── fresh-start launch timing instrumentation (submit-box-clear-immediately) ──────────
    // _fleetLaunchPending (declared in FleetCockpit.cs) is raised the moment a fresh run is
    // spawned and must come back down even if the run is never confirmed; this records WHEN it
    // went up so WatchFreshFleetLaunch can time it out instead of leaving Start wedged forever.
    DateTime _fleetLaunchPendingSinceUtc = DateTime.MinValue;
    // The exact text that was in the composer when the fresh run was spawned. The box is cleared
    // at spawn now (not at accept), so if the launch fails we put THIS back -- but only if the
    // operator has not meanwhile typed something new (see SubmitRestoreBox).
    string _fleetLaunchSubmittedText = "";

    // How long _fleetLaunchPending may stay up without the fresh run being confirmed before we
    // give up on it. Matched to the runner's own --wait-for-state-dir-seconds 60 (SpawnFleet),
    // which is the longest the spawned process legitimately takes to write its first snapshot;
    // a couple of seconds of slack is added so the timeout cannot beat a run that is about to
    // be accepted.
    static readonly TimeSpan _freshLaunchConfirmWindow = TimeSpan.FromSeconds(62);

    // Timestamped one-liners to .fleet/submit_timing.jsonl, written the same no-BOM way as
    // autofix.jsonl/ui_errors.jsonl (File.AppendAllText + NoBomUtf8), so "when did the box
    // clear" can be measured after the fact. A trace that can break submit is worse than none.
    void SubmitDbg(string what)
    {
        try
        {
            string line = "{\"ts\":" + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()
                        + ",\"iso\":\"" + DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
                        + "\",\"what\":\"" + (what ?? "").Replace("\\", "/").Replace("\"", "'") + "\"}";
            string path = Path.Combine(Path.GetDirectoryName(ResolvePath(null)), "submit_timing.jsonl");
            File.AppendAllText(path, line + Environment.NewLine, NoBomUtf8);
        }
        catch (Exception) { }
    }

    // Put the spawned goal text back in the composer after a failed launch WITHOUT clobbering
    // anything the operator typed in the meantime. Chosen to APPEND (not overwrite) when the box
    // is non-empty and different: this file already treats _goalInput as operator-owned (the
    // accept path only clears it when it still equals what was submitted), so silently replacing
    // freshly typed text would be the one destructive move the rest of the code avoids.
    void SubmitRestoreBox(string submittedText)
    {
        if (string.IsNullOrEmpty(submittedText)) return;
        string cur = _goalInput == null ? "" : (_goalInput.Text ?? "");
        if (cur.Length == 0)
        {
            if (_goalInput != null) _goalInput.Text = submittedText;
            SubmitDbg("restore_box_into_empty");
        }
        else if (cur.Trim() == submittedText.Trim())
        {
            SubmitDbg("restore_box_already_present");   // unchanged since submit: nothing to do
        }
        else
        {
            // Operator typed new text: keep theirs, append the un-launched goal below it.
            if (_goalInput != null) _goalInput.Text = cur.TrimEnd() + "\n" + submittedText;
            SubmitDbg("restore_box_appended_below_new_input");
        }
    }

    void NoteSubmitted(IEnumerable<string> goals)
    {
        NoteSubmitted(goals, CaptureSubmissionBaseline());
    }

    void NoteSubmitted(IEnumerable<string> goals, SubmissionBaseline baseline)
    {
        try
        {
            SubmissionBaseline b = baseline ?? CaptureSubmissionBaseline();
            var goalList = new List<string>();
            foreach (string g in goals) goalList.Add(SubmittedTasks.GoalTextOf(g));
            // Index the baseline ONCE for the whole batch (it used to be rebuilt per goal), and do
            // it here, before touching _submitted, so a background caller pays for it off the UI thread.
            var index = SubmittedTasks.BuildBaselineIndex(b.Started, b.Workers, b.History);
            Action apply = delegate
            {
                foreach (string g in goalList)
                    _submitted.AddLocal(g, b.CapturedUnix, index);
                ForceRender();
            };
            // _submitted belongs to the UI thread. On it: add NOW (the row must be on top before
            // any file is read back) and re-render next turn; elsewhere: hand the add over.
            if (Dispatcher.CheckAccess())
            {
                foreach (string g in goalList)
                    _submitted.AddLocal(g, b.CapturedUnix, index);
                Dispatcher.BeginInvoke(new Action(ForceRender));
            }
            else Dispatcher.BeginInvoke(apply);
        }
        catch (Exception) { }
    }

    void StartFleet()
    {
        try
        {
            SubmitDbg("start_click");
            if (_durableStartPending)
            {
                _startNote.Text = _lang == 0
                    ? "長時間タスクの開始確認中です。入力は保持されています。"
                    : "Waiting for the durable task to be accepted; your input is kept.";
                return;
            }
            if (_fleetLaunchPending)
            {
                _startNote.Text = _lang == 0
                    ? "前の実行終了を待って新しいタスクを開始中です。"
                    : "Waiting for the previous run to finish closing before starting this task.";
                return;
            }
            // refuse if a fleet is already running (both would write the same status.json)
            Dictionary<string, object> st = ReadStatus();
            if (st != null && st.ContainsKey("running") && Convert.ToBoolean(st["running"])
                && !(st.ContainsKey("idle") && Convert.ToBoolean(st["idle"])))
            {
                _startNote.Text = _lang == 0 ? "実行中です。完了後に。" : "Already running.";
                return;
            }
            var goals = new List<string>();
            foreach (string ln in (_goalInput.Text ?? "").Replace("\r", "").Split('\n'))
            {
                string s = ln.Trim();
                if (s.Length > 0 && !s.StartsWith("#")) goals.Add(s);
            }
            if (goals.Count == 0)
            {
                _startNote.Text = _lang == 0 ? "ゴールを入力してください。" : "Enter goals (one per line).";
                return;
            }
            foreach (string goal in goals)
            {
                if (!IsLocalLoopControlGoal(goal)) continue;
                if (_startNote != null) _startNote.Text = _lang == 0
                    ? "LOCAL_LOOP の内部制御文は通常タスクとして再実行できません。耐久タスクのカードから再開してください。"
                    : "LOCAL_LOOP control text cannot run as ordinary Fleet work. Resume the durable task card instead.";
                return;
            }
            if (goals.Count == 1 && goals[0].Equals("/help", StringComparison.OrdinalIgnoreCase))
            {
                _goalInput.Text = "";
                ShowGoalHelp();
                _startNote.Text = _lang == 0 ? "コマンド一覧を表示しました。" : "Command help shown.";
                return;
            }
            // Slash settings are handled by the shared HandleSlashSetting(), which the
            // send button also calls before deciding whether to start or add to a live run.
            if (HandleSlashSetting()) return;

            if (_runtimeMode == "durable")
            {
                if (goals.Count != 1)
                {
                    _startNote.Text = _lang == 0 ? "長時間実行モードは現在1タスクずつ開始します。1件にまとめてください。"
                                                  : "Durable runtime currently starts one task at a time.";
                    return;
                }
                if (_approval != "run")
                {
                    _startNote.Text = _lang == 0 ? "長時間実行モードのPhase 1は /approval run が必要です。"
                                                  : "Durable Phase 1 requires /approval run.";
                    return;
                }
                if (!DurableRuntimeEnabled())
                {
                    _startNote.Text = _lang == 0 ? "長時間実行は無効です。MCP_EXECUTION_PROFILES=1 を設定してMCPサーバを再起動してください。"
                                                  : "Durable runtime is disabled. Set MCP_EXECUTION_PROFILES=1 and restart the MCP server.";
                    return;
                }
                string durableSubmittedText = _goalInput.Text ?? "";
                if (!SpawnDurableTask(goals[0], durableSubmittedText)) return;
                _startNote.Text = _lang == 0
                    ? "長時間タスクの開始を確認中です。入力は保持されています。"
                    : "Starting durable task; your input is kept until LOCAL_LOOP accepts it.";
                _lastSig = "";
                return;
            }

            bool planMode = _approval == "plan" || _approval == "auto";
            string submittedText = _goalInput.Text ?? "";
            string freshFile = "fresh_start_" + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString()
                             + "_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".jsonl";
            if (!SpawnFleet(goals, freshFile, planMode, waitForClosingRun: true,
                            submittedText: submittedText)) return;
            _startNote.Text = _lang == 0
                ? "前の実行終了を待って開始します。タスクは保持されています。"
                : "Waiting for the previous run to close; the task is kept until the new run starts.";
            _lastSig = "";
        }
        catch (Exception ex)
        {
            _startNote.Text = (_lang == 0 ? "起動失敗: " : "Failed: ") + ex.Message;
        }
    }

    bool ActiveRunIsLocalLoop(Dictionary<string, object> st)
    {
        try
        {
            return st != null && string.Equals(
                S(st, "execution_mode"), "LOCAL_LOOP", StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception) { return false; }
    }

    bool ActiveRunIsLocalLoop()
    {
        try { return ActiveRunIsLocalLoop(ReadStatus()); }
        catch (Exception) { return false; }
    }

    void TryAddGoalsToActiveRun()
    {
        // LOCAL_LOOP and classic Fleet intentionally have different durable command channels.
        // LOCAL_LOOP additions become campaign jobs in SQLite/manifest; classic Fleet additions
        // keep using the one-command-per-file channel. Never let either side consume the other.
        if (ActiveRunIsLocalLoop())
        {
            TryAddGoalsToDurableRun();
            return;
        }
        TryAddGoalsToLiveFleet();
    }

    void TryAddGoalsToDurableRun()
    {
        if (_goalInput == null) return;
        if (_durableEnqueuePending)
        {
            if (_startNote != null)
                _startNote.Text = _lang == 0 ? "前の長時間タスクをキューへ登録中です。"
                                              : "The previous durable task submission is still being queued.";
            return;
        }
        var goals = new List<string>();
        foreach (string ln in (_goalInput.Text ?? "").Replace("\r", "").Split('\n'))
        {
            string goal = ln.Trim();
            if (goal.Length > 0 && !goal.StartsWith("#")) goals.Add(goal);
        }
        if (goals.Count == 0) return;

        SubmissionBaseline submitBaseline = CaptureSubmissionBaseline();
        string submittedText = _goalInput.Text ?? "";
        string goalsFile = null;
        System.Diagnostics.Process proc = null;
        try
        {
            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            string stateDir = Path.GetDirectoryName(_statusPath);
            string token = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString() + "_"
                         + Guid.NewGuid().ToString("N").Substring(0, 8);
            goalsFile = Path.Combine(stateDir, "durable_enqueue_" + token + ".json");
            File.WriteAllText(goalsFile, _js.Serialize(goals), new UTF8Encoding(false));

            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m relay.local_loop_controller --enqueue-goals-file \"" + goalsFile
                          + "\" --state-dir \"" + stateDir + "\"";
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            proc = System.Diagnostics.Process.Start(psi);
            if (proc == null) throw new InvalidOperationException("durable enqueue process did not start");
            _durableEnqueuePending = true;
            if (_startNote != null)
                _startNote.Text = _lang == 0 ? "長時間タスクをdurable queueへ登録中..."
                                              : "Queueing durable task(s)...";

            var timer = new System.Windows.Threading.DispatcherTimer();
            timer.Interval = TimeSpan.FromMilliseconds(200);
            timer.Tick += delegate
            {
                try
                {
                    proc.Refresh();
                    if (!proc.HasExited) return;
                    timer.Stop();
                    string stderr = "";
                    try { stderr = proc.StandardError.ReadToEnd(); } catch (Exception) { }
                    _durableEnqueuePending = false;
                    try { if (!string.IsNullOrEmpty(goalsFile) && File.Exists(goalsFile)) File.Delete(goalsFile); }
                    catch (Exception) { }
                    if (proc.ExitCode == 0)
                    {
                        NoteSubmitted(goals, submitBaseline);
                        if (_goalInput.Text == submittedText) _goalInput.Text = "";
                        if (_startNote != null)
                            _startNote.Text = _lang == 0 ? (goals.Count + " 件を長時間実行キューへ追加しました。")
                                                          : ("Queued " + goals.Count + " durable task(s)." );
                        _lastSig = "";
                    }
                    else
                    {
                        if (_startNote != null)
                            _startNote.Text = (_lang == 0 ? "長時間タスクのキュー登録に失敗。入力は残しています。 "
                                                          : "Durable queue submission failed; input was kept. ")
                                            + (stderr ?? "").Trim();
                    }
                    try { proc.Dispose(); } catch (Exception) { }
                }
                catch (Exception ex)
                {
                    timer.Stop();
                    _durableEnqueuePending = false;
                    if (_startNote != null)
                        _startNote.Text = (_lang == 0 ? "長時間タスクのキュー確認に失敗。入力は残しています。 "
                                                      : "Could not confirm durable queue submission; input was kept. ")
                                        + ex.Message;
                }
            };
            timer.Start();
        }
        catch (Exception ex)
        {
            _durableEnqueuePending = false;
            try { if (!string.IsNullOrEmpty(goalsFile) && File.Exists(goalsFile)) File.Delete(goalsFile); }
            catch (Exception) { }
            if (_startNote != null)
                _startNote.Text = (_lang == 0 ? "長時間タスクのキュー起動に失敗。入力は残しています。 "
                                              : "Could not start durable queue submission; input was kept. ")
                                + ex.Message;
        }
    }

    // The bottom composer is the task intake surface in BOTH idle and live-run states.
    // During a live run, enqueue new work through the same lossless one-command-per-file channel
    // used by retry. Worker-specific steering remains available on each worker card.
    // goal-set hash -> path of the add_goal command written for it (see TryAddGoalsToLiveFleet).
    readonly Dictionary<string, string> _pendingAddCommands = new Dictionary<string, string>();

    static string AddGoalSetKey(List<string> goals)
    {
        var parts = new List<string>();
        foreach (string g in goals) parts.Add(GoalKey(g));
        return string.Join("|", parts.ToArray());
    }

    void TryAddGoalsToLiveFleet()
    {
        if (_goalInput == null) return;
        var goals = new List<string>();
        foreach (string ln in (_goalInput.Text ?? "").Replace("\r", "").Split('\n'))
        {
            string goal = ln.Trim();
            if (goal.Length > 0 && !goal.StartsWith("#")) goals.Add(goal);
        }
        if (goals.Count == 0) return;
        foreach (string goal in goals)
        {
            if (!IsLocalLoopControlGoal(goal)) continue;
            if (_startNote != null) _startNote.Text = _lang == 0
                ? "LOCAL_LOOP の内部制御文は通常Fleetへ追加できません。耐久タスクのカードから再開してください。"
                : "LOCAL_LOOP control text cannot be added to ordinary Fleet. Resume the durable task card instead.";
            return;
        }
        // ONE UNCONSUMED COMMAND PER GOAL SET. A retry of the same text while its first command is
        // still sitting in commands.d (the runner's drain lagged 30-190 s on 2026-10-14) used to
        // write a SECOND command, and the runner admits identical text under a new command as a
        // new task: 3 goals became 5 workers. The key is the goal-set hash; the first command's
        // file existing on disk is the proof it has not been consumed yet.
        string addKey = AddGoalSetKey(goals);
        string pendingPath;
        if (_pendingAddCommands.TryGetValue(addKey, out pendingPath))
        {
            if (!string.IsNullOrEmpty(pendingPath) && File.Exists(pendingPath))
            {
                _goalInput.Text = "";
                if (_startNote != null)
                    _startNote.Text = _lang == 0 ? "同じタスクは既にキューに入っています(処理待ち)。重複追加しません。"
                                                  : "That task is already queued (waiting to be picked up); not adding a duplicate.";
                return;
            }
            _pendingAddCommands.Remove(addKey);
        }
        SubmissionBaseline submitBaseline = CaptureSubmissionBaseline();

        // This method is entered from the ACTIVE composer. Do not re-decide ownership from one
        // status read here: a transient unreadable/stale snapshot used to divert this submission
        // into StartFleet(), where the state-dir lock correctly rejected the second coordinator
        // after the UI had already cleared the input. Always land the tracked command; its applied
        // receipt + WatchLiveAddHandoff resolve whether the old run consumes it or a new run adopts it.

        // WHO SUBMITTED, AND BY WHICH ROUTE. The cockpit's "追加" wrote an add_goal with no
        // provenance, so a goal that joined a live run read origin=null while the same goal
        // through fleet_submit carried its origin into history.json. Stamp it here in the same
        // {via, source} shape fleet_intake uses, so goals_from_command threads it onto the goal
        // and it reaches the worker row and history.json. via "ui_add" tells this apart from an
        // mcp/cli goal joining a live run; source names the machine user and this process so the
        // submitter is recoverable. Built once for the whole batch.
        var uiOrigin = new Dictionary<string, object>();
        uiOrigin["via"] = "ui_add";
        uiOrigin["source"] = "cockpit " + Environment.UserName + " pid "
                            + System.Diagnostics.Process.GetCurrentProcess().Id;
        var adds = new List<object>();
        foreach (string goal in goals)
        {
            var item = new Dictionary<string, object>();
            item["text"] = goal;
            item["priority"] = false;
            item["origin"] = uiOrigin;
            adds.Add(item);
        }
        var patch = Cmd1("add_goal", adds);
        string ackId = "ui-live-" + Guid.NewGuid().ToString("N");
        string ackPath = Path.Combine(_fleetDir, "acks", ackId + ".ack");
        patch["ack"] = ackPath;
        string commandPath;
        if (!SendTrackedCommand(patch, out commandPath))
        {
            if (_startNote != null)
                _startNote.Text = _lang == 0 ? "タスクをキューへ追加できませんでした。入力は残っています。"
                                              : "Could not queue the task. Your input was kept.";
            return;
        }

        _pendingAddCommands[addKey] = commandPath;
        // Optimistic row first; the runner will replace it with a real worker on the next sweep.
        NoteSubmitted(goals, submitBaseline);
        WatchLiveAddHandoff(commandPath, ackPath);
        _goalInput.Text = "";
        if (_startNote != null)
            _startNote.Text = _lang == 0 ? (goals.Count + " 件を実行中のキューへ追加しました。")
                                          : ("Queued " + goals.Count + " task(s) into the active run.");
        _lastSig = "";
    }

    // 1 = the runner durably applied the command, 0 = receipt not ready/parseable yet,
    // -1 = the runner explicitly rejected it. Mere file existence is NOT success: rejected
    // commands also have receipts, and treating those as success silently lost a submitted task.
    int LiveAddReceiptState(string ackPath, out string detail)
    {
        detail = "";
        if (string.IsNullOrEmpty(ackPath) || !File.Exists(ackPath)) return 0;
        try
        {
            var d = _js.DeserializeObject(File.ReadAllText(ackPath, Encoding.UTF8))
                    as Dictionary<string, object>;
            if (d == null) return 0;
            bool rejected = d.ContainsKey("rejected") && Convert.ToBoolean(d["rejected"]);
            if (rejected)
            {
                detail = _lang == 0 ? "タスク追加がrunnerに拒否されました。" : "The runner rejected the added task.";
                return -1;
            }
            if (d.ContainsKey("applied"))
            {
                bool applied = Convert.ToBoolean(d["applied"]);
                if (applied) return 1;
                detail = _lang == 0 ? "タスク追加は適用されませんでした。" : "The added task was not applied.";
                return -1;
            }
        }
        catch (Exception)
        {
            // Atomic receipt writes should make parse failures rare; treat one as incomplete and
            // retry instead of converting an ambiguous file into success or failure.
        }
        return 0;
    }

    // A live add can race the final sweep: the UI saw running=true, wrote the command, then the
    // coordinator finished before its next drain.  The command is durable now, so do not guess
    // from status alone.  Receipt = applied/accepted.  No receipt + no live run = launch a
    // rescuer that adopts THIS exact pending command after winning fleet_runner's state-dir lock.
    void WatchLiveAddHandoff(string commandPath, string ackPath)
    {
        if (string.IsNullOrEmpty(commandPath) || string.IsNullOrEmpty(ackPath)) return;
        var timer = new System.Windows.Threading.DispatcherTimer();
        timer.Interval = TimeSpan.FromMilliseconds(300);
        DateTime lastRescue = DateTime.MinValue;
        // BOUNDED. This timer used to run until a receipt appeared and spawned a python process
        // every 2 s whenever the status looked not-live (idle, or stale past 150 s -- which a slow
        // drain produces), one process per pending command, forever. A losing rescuer only exits 3,
        // but each costs a venv python start. At most 3 rescuers per command, 30 s apart, and the
        // watch ends when the command file is gone (the runner claimed it: the runner, not this
        // timer, owns it from then on) or after 15 minutes.
        int rescueCount = 0;
        DateTime watchStart = DateTime.UtcNow;
        timer.Tick += delegate
        {
            try
            {
                string receiptError;
                int receiptState = LiveAddReceiptState(ackPath, out receiptError);
                if (receiptState > 0) { timer.Stop(); return; }
                if ((DateTime.UtcNow - watchStart).TotalMinutes > 15.0) { timer.Stop(); return; }
                if (receiptState == 0 && !File.Exists(commandPath)) return; // claimed; receipt follows
                if (receiptState < 0)
                {
                    timer.Stop();
                    if (_startNote != null) _startNote.Text = receiptError;
                    _lastSig = "";
                    return;
                }
                if (RunIsLive()) return;
                // Retry, rather than fire once: a dying old coordinator may still own the OS
                // lock for a moment. A losing rescuer exits 3 before touching commandPath.
                if ((DateTime.UtcNow - lastRescue).TotalSeconds < 30.0) return;
                if (rescueCount >= 3) return;
                lastRescue = DateTime.UtcNow;
                rescueCount++;
                SpawnFleetAdoptCommand(commandPath);
                if (_startNote != null)
                    _startNote.Text = _lang == 0
                        ? "実行終了と同時に追加されたタスクを、新しい実行へ引き継いでいます。"
                        : "The run ended during submission; carrying the pending task into a new run.";
            }
            catch (Exception)
            {
                // Keep the durable command and retry on the next tick. Losing the watcher must
                // never mean losing the task.
            }
        };
        timer.Start();
    }

    // Spawn a fresh `python -m relay.fleet_runner` for the given goal texts. Factored out of
    // StartFleet so RETRY can reuse it: when a run has FINISHED, the cockpit relaunches a fleet
    // (instead of writing an add_goal that nothing alive would ever consume). The agent URL is
    // NOT passed -- the runner resolves it from MCP_FLEET_AGENT_URL / .env, exactly as a manual
    // Start does -- and --state-dir is the same .fleet dir this cockpit tails, so the relaunched
    // run shows up live here. Goals are handed over via a UTF-8 file, written as JSONL (one JSON
    // object per line -- see GoalsToJsonl) so a goal string containing embedded newlines survives
    // as a SINGLE goal instead of being shredded one-line-per-fragment by fleet_runner's
    // line-based reader (incident: a multi-line review prompt split into 53 nonsense goals).
    // Returns true if the process started. `goalsFileName` lets callers use a distinct file so a
    // retry spawn never clobbers the manual Start input file (or vice-versa).
    // Fire-and-forget: shell out to relay.fleet_reaper.reap_stale_run() so a phantom run's
    // status.json/history.json/fleet_run_active.json get finalized to a clean cancelled state,
    // not just the cockpit's in-memory view (see ArchiveAllStale's call site above). Mirrors
    // SpawnFleet's ProcessStartInfo shape. Never blocks the UI thread, never throws.
    void ReapStaleFleetRun()
    {
        try
        {
            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";

            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-c \"from relay.fleet_reaper import reap_stale_run; reap_stale_run()\"";
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            System.Diagnostics.Process.Start(psi);
        }
        catch (Exception) { }
    }

    bool DurableRuntimeEnabled()
    {
        string raw = Environment.GetEnvironmentVariable("MCP_EXECUTION_PROFILES") ?? "";
        try
        {
            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string envPath = Path.Combine(repo, ".env");
            if (string.IsNullOrWhiteSpace(raw) && File.Exists(envPath))
            {
                foreach (string line in File.ReadAllLines(envPath))
                {
                    string trimmed = (line ?? "").Trim();
                    if (trimmed.StartsWith("MCP_EXECUTION_PROFILES=", StringComparison.OrdinalIgnoreCase))
                    {
                        raw = trimmed.Substring(trimmed.IndexOf('=') + 1).Trim().Trim('"', (char)39);
                        break;
                    }
                }
            }
        }
        catch (Exception) { }
        raw = (raw ?? "").Trim().ToLowerInvariant();
        return raw == "1" || raw == "true" || raw == "yes" || raw == "on";
    }

    bool ResumeLocalLoopRuntime(Dictionary<string, object> w)
    {
        try
        {
            if (w == null) return false;
            string jobId = S(w, "name").Replace("\"", "");
            string db = S(w, "local_job_db").Replace("\"", "");
            if (string.IsNullOrWhiteSpace(jobId)) return false;
            if (!DurableRuntimeEnabled())
            {
                if (_startNote != null)
                    _startNote.Text = _lang == 0
                        ? "長時間実行は無効です。MCP_EXECUTION_PROFILES=1 を設定してください。"
                        : "Durable runtime is disabled. Set MCP_EXECUTION_PROFILES=1 first.";
                return false;
            }

            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            string stateDir = Path.GetDirectoryName(_statusPath);
            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m relay.local_loop_controller --job-id \"" + jobId
                          + "\" --resume-runtime --state-dir \"" + stateDir + "\"";
            if (!string.IsNullOrWhiteSpace(db)) psi.Arguments += " --db \"" + db + "\"";
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            var proc = System.Diagnostics.Process.Start(psi);
            if (proc == null) throw new InvalidOperationException("LOCAL_LOOP resume process did not start");
            if (_startNote != null)
                _startNote.Text = _lang == 0
                    ? "実行環境の修復後として、同じ長時間タスクを再開しています。"
                    : "Resuming the same durable task after runtime repair.";
            _lastSig = "";
            return true;
        }
        catch (Exception ex)
        {
            if (_startNote != null)
                _startNote.Text = (_lang == 0 ? "長時間タスクの再開に失敗: " : "Durable resume failed: ") + ex.Message;
            return false;
        }
    }

    bool SpawnDurableTask(string goal, string submittedText)
    {
        SubmissionBaseline submitBaseline = CaptureSubmissionBaseline();
        string goalFile = null;
        System.Diagnostics.Process proc = null;
        try
        {
            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            string stateDir = Path.GetDirectoryName(_statusPath);
            string token = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString() + "_"
                         + Guid.NewGuid().ToString("N").Substring(0, 8);
            goalFile = Path.Combine(stateDir, "durable_goal_" + token + ".txt");
            File.WriteAllText(goalFile, goal ?? "", new UTF8Encoding(false));

            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m relay.local_loop_controller --goal-file \"" + goalFile
                          + "\" --state-dir \"" + stateDir + "\"";
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            proc = System.Diagnostics.Process.Start(psi);
            if (proc == null) throw new InvalidOperationException("durable controller process did not start");
            _durableStartPending = true;
            NoteSubmitted(new List<string> { goal }, submitBaseline);
            WatchDurableStart(proc, submitBaseline, goalFile, submittedText ?? "", goal);
            return true;
        }
        catch (Exception ex)
        {
            _durableStartPending = false;
            try { if (!string.IsNullOrEmpty(goalFile) && File.Exists(goalFile)) File.Delete(goalFile); }
            catch (Exception) { }
            if (_startNote != null)
                _startNote.Text = (_lang == 0 ? "長時間実行の起動に失敗: " : "Durable start failed: ") + ex.Message;
            try { if (proc != null) proc.Dispose(); } catch (Exception) { }
            return false;
        }
    }

    void WatchDurableStart(System.Diagnostics.Process proc, SubmissionBaseline baseline,
                           string goalFile, string submittedText, string goal)
    {
        var timer = new System.Windows.Threading.DispatcherTimer();
        timer.Interval = TimeSpan.FromMilliseconds(200);
        DateTime exitSeenAt = DateTime.MinValue;
        timer.Tick += delegate
        {
            try
            {
                Dictionary<string, object> root = ReadStatus();
                string started = StartedOf(root);
                bool accepted = ActiveRunIsLocalLoop(root)
                                && !string.IsNullOrEmpty(started)
                                && !string.Equals(started, baseline == null ? "" : baseline.Started,
                                                  StringComparison.Ordinal)
                                && FreshRunContainsGoals(root, new List<string> { goal });
                if (accepted)
                {
                    timer.Stop();
                    _durableStartPending = false;
                    if (_goalInput.Text == submittedText) _goalInput.Text = "";
                    try { if (!string.IsNullOrEmpty(goalFile) && File.Exists(goalFile)) File.Delete(goalFile); }
                    catch (Exception) { }
                    if (_startNote != null)
                        _startNote.Text = _lang == 0
                            ? "長時間タスクを開始しました。現在の工程と進捗をカードに表示します。"
                            : "Durable task started. Current step and progress will appear on the card.";
                    _lastSig = "";
                    try { proc.Dispose(); } catch (Exception) { }
                    return;
                }

                proc.Refresh();
                if (!proc.HasExited) return;
                if (exitSeenAt == DateTime.MinValue)
                {
                    exitSeenAt = DateTime.UtcNow;
                    return;
                }
                if ((DateTime.UtcNow - exitSeenAt).TotalSeconds < 2.0) return;

                timer.Stop();
                _durableStartPending = false;
                int code = -1;
                try { code = proc.ExitCode; } catch (Exception) { }
                try { if (!string.IsNullOrEmpty(goalFile) && File.Exists(goalFile)) File.Delete(goalFile); }
                catch (Exception) { }
                if (_startNote != null)
                    _startNote.Text = (_lang == 0
                        ? "長時間タスクを開始できませんでした。入力は残しています。終了コード "
                        : "Could not start durable task; input was kept. Exit code ") + code;
                try { proc.Dispose(); } catch (Exception) { }
            }
            catch (Exception ex)
            {
                timer.Stop();
                _durableStartPending = false;
                if (_startNote != null)
                    _startNote.Text = (_lang == 0
                        ? "長時間タスクの開始確認に失敗しました。入力は残しています。 "
                        : "Could not confirm durable task start; input was kept. ") + ex.Message;
                try { proc.Dispose(); } catch (Exception) { }
            }
        };
        timer.Start();
    }

    bool SpawnFleet(List<string> goals, string goalsFileName, bool planMode = false,
                    bool waitForClosingRun = false, string submittedText = null,
                    SubmissionBaseline preBaseline = null)
    {
        foreach (string rawGoal in (goals ?? new List<string>()))
        {
            if (!IsLocalLoopControlGoal(SubmittedTasks.GoalTextOf(rawGoal))) continue;
            string loopNote = _lang == 0
                ? "LOCAL_LOOP の内部制御文は通常Fleetとして起動できません。耐久タスクのカードから再開してください。"
                : "LOCAL_LOOP control text cannot start ordinary Fleet work. Resume the durable task card instead.";
            // May run on a worker thread (bulk retry): _startNote belongs to the UI thread.
            Dispatcher.BeginInvoke(new Action(delegate { if (_startNote != null) _startNote.Text = loopNote; }));
            return false;
        }
        // A caller that already captured the baseline on the UI thread (so this body can run
        // off it) passes it in; the snapshot must still predate the handoff below.
        SubmissionBaseline submitBaseline = preBaseline ?? CaptureSubmissionBaseline();
        string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
        string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
        if (!File.Exists(py)) py = "python";
        string stateDir = Path.GetDirectoryName(_statusPath);
        string goalsFile = Path.Combine(stateDir, goalsFileName);
        File.WriteAllText(goalsFile, GoalsToJsonl(goals), new UTF8Encoding(false));

        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = py;
        psi.Arguments = "-m relay.fleet_runner --goals-file \"" + goalsFile + "\""
                        + " --state-dir \"" + stateDir + "\" --effort " + _effort;
        // THE "開始" DOOR NAMES ITSELF. This start reaches the fleet through --goals-file, not
        // commands.d, so the runner records its admission from these two flags BEFORE the run
        // lock -- the one place a start refused for a held lock (exit 3) still leaves a receipt,
        // and where "which screen/session asked" survives instead of being journalled as "cli".
        // Same {route, submitter} shape as the "追加" button's uiOrigin above.
        psi.Arguments += " --submit-route ui_start --submit-origin \"cockpit "
                        + Environment.UserName + " pid "
                        + System.Diagnostics.Process.GetCurrentProcess().Id + "\"";
        if (planMode) psi.Arguments += " --plan";
        if (waitForClosingRun) psi.Arguments += " --wait-for-state-dir-seconds 60";
        // BOTH VALUES ARE SAID OUT LOUD. This used to append "--fanout" when on and nothing
        // when off -- and once the runner's default became true, saying nothing meant ON, so
        // a person who typed /fanout off got fan-out anyway while the cockpit reported OFF.
        // A boolean expressed by the presence of a flag can only state one of its two values.
        psi.Arguments += _fanout ? " --fanout" : " --no-fanout";
        psi.WorkingDirectory = repo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        // CAPTURE THE COORDINATOR'S STDOUT/STDERR. Two defects are fixed here together:
        //  - (gui_start) a GUI-launched coordinator that printed "TIMED OUT WAITING TO START"
        //    and exited 3 left no trace anywhere, so the operator saw a stuck "投入済み" card
        //    with no way to learn why. Redirect BOTH streams and drain them asynchronously (a
        //    sync ReadToEnd here would block the UI thread / risk a pipe deadlock) into
        //    .fleet/gui_start_<time>.log.
        //  - (refused_ux) a run that fails to start said only its exit code (or nothing), so the
        //    person at the machine was told "could not start" with no reason. In addition to the
        //    log file, stderr is drained into a bounded in-memory buffer so WatchFreshFleetLaunch
        //    can put the runner's OWN last words on screen when it dies.
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
        string launchLogPath = Path.Combine(stateDir, "gui_start_"
                             + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString() + ".log");
        System.Diagnostics.Process proc = System.Diagnostics.Process.Start(psi);
        if (proc == null) return false;
        // Drain both streams to the log file (gui_start) AND keep stderr's tail in a bounded
        // in-memory buffer (refused_ux) so the failure reason can also reach the operator's card.
        StringBuilder errBuf = new StringBuilder();
        try
        {
            var logLock = new object();
            System.IO.StreamWriter logw = null;
            try { logw = new System.IO.StreamWriter(launchLogPath, true, new UTF8Encoding(false)); }
            catch (Exception) { logw = null; }
            System.Diagnostics.DataReceivedEventHandler sink = delegate (object s2, System.Diagnostics.DataReceivedEventArgs e)
            {
                if (e == null || e.Data == null || logw == null) return;
                try { lock (logLock) { logw.WriteLine(e.Data); logw.Flush(); } } catch (Exception) { }
            };
            proc.OutputDataReceived += sink;
            proc.ErrorDataReceived += delegate (object s, System.Diagnostics.DataReceivedEventArgs e)
            {
                // Log file (via sink) AND the bounded tail buffer: keep only the TAIL, since a
                // runaway traceback must not grow unbounded in the UI process and the last lines
                // are the ones that name the failure.
                sink(s, e);
                if (e == null || e.Data == null) return;
                lock (errBuf)
                {
                    if (errBuf.Length < 8000) errBuf.Append(e.Data).Append('\n');
                }
            };
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            // Close the log writer when the coordinator exits so the handle is not leaked.
            proc.EnableRaisingEvents = true;
            proc.Exited += delegate { try { lock (logLock) { if (logw != null) { logw.Flush(); logw.Dispose(); logw = null; } } } catch (Exception) { } };
        }
        catch (Exception) { }
        SubmitDbg("spawn_ok waitForClosingRun=" + waitForClosingRun);
        // WHERE THE "投入済み" CARD IS RAISED.
        // For the fresh-Start path (waitForClosingRun) the card is NOT raised here: the coordinator
        // may still time out waiting for the previous run's lock (exit 3) and never accept this
        // submission, and a card raised at spawn time then stuck forever with no run behind it
        // (gui_start fix). WatchFreshFleetLaunch raises it the moment the new run is confirmed
        // accepted, and -- below -- retries or stands down on timeout. The composer box, however,
        // is emptied NOW on spawn success (ui_submit fix) so the operator is not left staring at
        // text that looks un-sent; a failed/timed-out launch restores it. Because the card is not
        // raised yet, the submitted text is surfaced in the start-note (its leading chars) so the
        // work stays visible to the operator during the "waiting to start" gap.
        // Non-waiting spawns (retry / bulk retry / continue with no live run) do NOT watch, so
        // they raise the card now, as before.
        if (waitForClosingRun)
        {
            _fleetLaunchPending = true;
            _fleetLaunchPendingSinceUtc = DateTime.UtcNow;
            _fleetLaunchSubmittedText = submittedText ?? "";
            // Box clears NOW, at spawn success -- not when the fresh run is confirmed. The card is
            // raised later (at acceptance), so clearing only empties the composer (if it still holds
            // exactly what we spawned); a failed/timed-out launch restores it.
            if (_goalInput != null && _goalInput.Text == (submittedText ?? ""))
            {
                _goalInput.Text = "";
                SubmitDbg("box_cleared_at_spawn");
            }
            WatchFreshFleetLaunch(proc, submitBaseline, goalsFile, submittedText ?? "",
                                  goals, planMode, errBuf, 0);
        }
        else
        {
            // Non-waiting spawn paths do not watch for acceptance, so the "投入済み" card must be
            // raised here -- there is no later point that would raise it.
            NoteSubmitted(goals, submitBaseline);
            try { proc.Dispose(); } catch (Exception) { }
        }
        return true;
    }

    // The runner's last stderr lines, trimmed to a single short line for the start note. Empty
    // when it said nothing. Static and side-effect free so a test can feed it a buffer.
    static string StartFailureReason(StringBuilder errBuf)
    {
        if (errBuf == null) return "";
        string all;
        lock (errBuf) { all = errBuf.ToString(); }
        if (string.IsNullOrEmpty(all)) return "";
        string[] lines = all.Replace("\r", "").Split('\n');
        string last = "";
        for (int i = lines.Length - 1; i >= 0; i--)
        {
            if (lines[i] != null && lines[i].Trim().Length > 0) { last = lines[i].Trim(); break; }
        }
        if (last.Length > 300) last = last.Substring(0, 300) + "…";
        return last;
    }

    static bool FreshRunContainsGoals(Dictionary<string, object> root, List<string> goals)
    {
        if (root == null || goals == null || goals.Count == 0) return false;
        List<Dictionary<string, object>> workers = WorkersOf(root);
        foreach (string raw in goals)
        {
            string wanted = SubmittedTasks.GoalTextOf(raw).Trim();
            bool found = false;
            foreach (Dictionary<string, object> w in workers)
            {
                if (string.Equals(S(w, "goal").Trim(), wanted, StringComparison.Ordinal))
                {
                    found = true;
                    break;
                }
            }
            if (!found) return false;
        }
        return true;
    }

    // Fresh-Start retry budget. The coordinator the GUI launches waits up to 60s
    // (--wait-for-state-dir-seconds 60) for a closing prior run to release the state-dir
    // lock; a prior run's cleanup was MEASURED at ~1m48s on 2026-10-26, longer than that 60s,
    // so the launched coordinator can exit 3 ("TIMED OUT WAITING TO START") through no fault of
    // the submission. The companion fix shortens the prior run's lock-hold to just after its
    // final persistence, so one more 60s attempt almost always lands; 2 total attempts bounds
    // the worst case at ~2 minutes. The 1.5s gap lets the timed-out process fully exit and its
    // lock clear before the retry spawns.
    const int FreshLaunchMaxAttempts = 2;
    static readonly TimeSpan FreshLaunchRetryGap = TimeSpan.FromMilliseconds(1500);

    void WatchFreshFleetLaunch(System.Diagnostics.Process proc, SubmissionBaseline baseline,
                               string goalsFile, string submittedText, List<string> goals, bool planMode,
                               StringBuilder errBuf, int attempt)
    {
        var timer = new System.Windows.Threading.DispatcherTimer();
        timer.Interval = TimeSpan.FromMilliseconds(200);
        DateTime exitSeenAt = DateTime.MinValue;
        timer.Tick += delegate
        {
            try
            {
                Dictionary<string, object> root = ReadStatus();
                string started = StartedOf(root);
                bool accepted = !string.IsNullOrEmpty(started)
                                && !string.Equals(started, baseline == null ? "" : baseline.Started,
                                                  StringComparison.Ordinal)
                                && FreshRunContainsGoals(root, goals);
                if (accepted)
                {
                    timer.Stop();
                    _fleetLaunchPending = false;
                    _fleetLaunchPendingSinceUtc = DateTime.MinValue;
                    // RAISE THE 投入済み CARD NOW -- the new run is confirmed accepted. Raising it at
                    // spawn time (as before) left a stuck card when the coordinator timed out waiting
                    // for the prior run's lock and never accepted this work (gui_start fix).
                    NoteSubmitted(goals, baseline);
                    // Box was already emptied at spawn success (ui_submit fix); nothing to clear here.
                    SubmitDbg("accepted goals=" + goals.Count);
                    try { if (File.Exists(goalsFile)) File.Delete(goalsFile); } catch (Exception) { }
                    if (_startNote != null)
                        _startNote.Text = (_lang == 0 ? "開始しました（" : "Started (") + goals.Count
                                          + (_lang == 0 ? " 件）" : " goals)")
                                          + (planMode
                                              ? (_lang == 0 ? "。承認待ちの計画が各カードに出ます。"
                                                            : ". Each card will wait at plan approval.")
                                              : "");
                    _lastSig = "";
                    try { proc.Dispose(); } catch (Exception) { }
                    return;
                }

                proc.Refresh();
                if (!proc.HasExited)
                {
                    // Neither confirmed nor exited. Bound the wait so _fleetLaunchPending cannot stay
                    // raised forever (ui_submit fix): past the confirm window, stand down and restore.
                    if (_fleetLaunchPendingSinceUtc != DateTime.MinValue
                        && (DateTime.UtcNow - _fleetLaunchPendingSinceUtc) > _freshLaunchConfirmWindow)
                    {
                        timer.Stop();
                        _fleetLaunchPending = false;
                        _fleetLaunchPendingSinceUtc = DateTime.MinValue;
                        SubmitDbg("timeout_unconfirmed");
                        try { if (File.Exists(goalsFile)) File.Delete(goalsFile); } catch (Exception) { }
                        SubmitRestoreBox(submittedText);
                        if (_startNote != null)
                            _startNote.Text = _lang == 0
                                ? "起動を確認できませんでした。入力は戻しました。"
                                : "Could not confirm the launch; your input was restored.";
                        try { proc.Dispose(); } catch (Exception) { }
                    }
                    return;
                }
                if (exitSeenAt == DateTime.MinValue)
                {
                    exitSeenAt = DateTime.UtcNow;
                    return;
                }
                if ((DateTime.UtcNow - exitSeenAt).TotalSeconds < 2.0) return;

                timer.Stop();
                int code = -1;
                try { code = proc.ExitCode; } catch (Exception) { }
                try { proc.Dispose(); } catch (Exception) { }
                SubmitDbg("spawn_exited_early code=" + code);

                // EXIT 3 == "TIMED OUT WAITING TO START": the prior run had not released the state-dir
                // lock within the coordinator's 60s wait. The submission was never accepted and no card
                // was raised, so retrying the SAME goals is safe. Retry up to the budget (gui_start fix).
                if (code == 3 && attempt + 1 < FreshLaunchMaxAttempts)
                {
                    try { if (File.Exists(goalsFile)) File.Delete(goalsFile); } catch (Exception) { }
                    if (_startNote != null)
                        _startNote.Text = (_lang == 0
                            ? "前の実行の後片付けを待っています。もう一度開始を試みます…"
                            : "Previous run is still closing; retrying the start...");
                    var retry = new System.Windows.Threading.DispatcherTimer();
                    retry.Interval = FreshLaunchRetryGap;
                    retry.Tick += delegate
                    {
                        retry.Stop();
                        string freshFile = "fresh_start_" + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString()
                                         + "_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".jsonl";
                        if (!SpawnFleetRetry(goals, freshFile, planMode, submittedText, baseline, attempt + 1))
                        {
                            _fleetLaunchPending = false;
                            _fleetLaunchPendingSinceUtc = DateTime.MinValue;
                            SubmitRestoreBox(submittedText);
                            if (_startNote != null)
                                _startNote.Text = (_lang == 0
                                    ? "新しい実行を開始できませんでした。入力は戻しました。"
                                    : "Could not start the new run; your input was restored.");
                        }
                    };
                    retry.Start();
                    return;
                }

                // Out of retries (or a non-timeout failure): stand down. The box was emptied at spawn,
                // so restore the operator's text (ui_submit fix) and say so.
                _fleetLaunchPending = false;
                _fleetLaunchPendingSinceUtc = DateTime.MinValue;
                try { if (File.Exists(goalsFile)) File.Delete(goalsFile); } catch (Exception) { }
                // The box was emptied at spawn (ui_submit fix), so restore the operator's text
                // (gui_start fix), then show the exit code AND the reason: StartFailureReason
                // returns the runner's last stderr line (empty if it was silent), appended so the
                // person sees WHY, not just the number (refused_ux fix).
                SubmitRestoreBox(submittedText);
                string why = StartFailureReason(errBuf);
                if (_startNote != null)
                    _startNote.Text = (_lang == 0
                        ? "新しい実行を開始できませんでした。入力は戻しました。終了コード "
                        : "Could not start the new run; your input was restored. Exit code ")
                        + code
                        + (why.Length > 0 ? (_lang == 0 ? "。理由: " : ". Reason: ") + why : "");
                try { proc.Dispose(); } catch (Exception) { }
            }
            catch (Exception ex)
            {
                timer.Stop();
                _fleetLaunchPending = false;
                _fleetLaunchPendingSinceUtc = DateTime.MinValue;
                SubmitDbg("confirm_failed " + ex.GetType().Name);
                SubmitRestoreBox(submittedText);
                if (_startNote != null)
                    _startNote.Text = (_lang == 0
                        ? "新しい実行の開始確認に失敗しました。入力は戻しました。 "
                        : "Could not confirm the new run start; your input was restored. ")
                        + ex.Message;
                try { proc.Dispose(); } catch (Exception) { }
            }
        };
        timer.Start();
    }

    // The retry arm of the fresh-Start path. Mirrors the spawn block inside SpawnFleet's
    // waitForClosingRun branch, but re-enters WatchFreshFleetLaunch carrying the incremented
    // attempt count and the ORIGINAL baseline, so the card still lands against the right
    // pre-submission snapshot. Kept separate so the primary SpawnFleet signature is unchanged.
    bool SpawnFleetRetry(List<string> goals, string goalsFileName, bool planMode,
                         string submittedText, SubmissionBaseline baseline, int attempt)
    {
        try
        {
            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            string stateDir = Path.GetDirectoryName(_statusPath);
            string goalsFile = Path.Combine(stateDir, goalsFileName);
            File.WriteAllText(goalsFile, GoalsToJsonl(goals), new UTF8Encoding(false));

            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m relay.fleet_runner --goals-file \"" + goalsFile + "\""
                            + " --state-dir \"" + stateDir + "\" --effort " + _effort;
            if (planMode) psi.Arguments += " --plan";
            psi.Arguments += " --wait-for-state-dir-seconds 60";
            psi.Arguments += _fanout ? " --fanout" : " --no-fanout";
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            string launchLogPath = Path.Combine(stateDir, "gui_start_"
                                 + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString() + ".log");
            System.Diagnostics.Process proc = System.Diagnostics.Process.Start(psi);
            if (proc == null) return false;
            // Same dual drain as the primary spawn path: both streams to the log file (gui_start)
            // and stderr's tail into a bounded buffer (refused_ux) so a retry that also fails can
            // still name WHY on the operator's card.
            StringBuilder errBuf = new StringBuilder();
            try
            {
                var logLock = new object();
                System.IO.StreamWriter logw = null;
                try { logw = new System.IO.StreamWriter(launchLogPath, true, new UTF8Encoding(false)); }
                catch (Exception) { logw = null; }
                System.Diagnostics.DataReceivedEventHandler sink = delegate (object s2, System.Diagnostics.DataReceivedEventArgs e)
                {
                    if (e == null || e.Data == null || logw == null) return;
                    try { lock (logLock) { logw.WriteLine(e.Data); logw.Flush(); } } catch (Exception) { }
                };
                proc.OutputDataReceived += sink;
                proc.ErrorDataReceived += delegate (object s, System.Diagnostics.DataReceivedEventArgs e)
                {
                    sink(s, e);
                    if (e == null || e.Data == null) return;
                    lock (errBuf)
                    {
                        if (errBuf.Length < 8000) errBuf.Append(e.Data).Append('\n');
                    }
                };
                proc.BeginOutputReadLine();
                proc.BeginErrorReadLine();
                proc.EnableRaisingEvents = true;
                proc.Exited += delegate { try { lock (logLock) { if (logw != null) { logw.Flush(); logw.Dispose(); logw = null; } } } catch (Exception) { } };
            }
            catch (Exception) { }
            _fleetLaunchPending = true;
            _fleetLaunchPendingSinceUtc = DateTime.UtcNow;
            _fleetLaunchSubmittedText = submittedText ?? "";
            WatchFreshFleetLaunch(proc, baseline, goalsFile, submittedText ?? "", goals, planMode, errBuf, attempt);
            return true;
        }
        catch (Exception) { return false; }
    }

    // Render a goals list as the JSONL text SpawnFleet writes to its goals-file: one JSON object
    // per physical line, `{"text":"<goal, JSON-escaped>"}`. Using JavaScriptSerializer (already
    // used elsewhere in this file, e.g. the continuation-goal encoding below) guarantees proper
    // escaping of embedded newlines/quotes/backslashes/control chars, so a multi-line goal string
    // round-trips as ONE goal instead of one bogus goal per source line (fleet_runner._read_goals
    // splits a plain-text goals-file on newlines).
    //
    // A goal that is ALREADY a serialized JSON object (callers that need extra keys beyond "text",
    // e.g. the "続ける"/Continue flow's {"text":...,"resume_conv":...}) is detected and passed
    // through UNCHANGED rather than being wrapped a second time -- double-wrapping would bury the
    // real text/resume_conv keys inside an opaque string and fleet_runner would lose resume_conv.
    string GoalsToJsonl(List<string> goals)
    {
        var sb = new StringBuilder();
        foreach (string g in (goals ?? new List<string>()))
        {
            string trimmed = (g ?? "").Trim();
            string line = null;
            if (trimmed.StartsWith("{"))
            {
                try
                {
                    var parsed = _js.DeserializeObject(trimmed) as Dictionary<string, object>;
                    if (parsed != null) line = trimmed; // already a valid JSON-object goal; pass through as-is
                }
                catch (Exception) { line = null; }
            }
            if (line == null)
            {
                var gd = new Dictionary<string, object>();
                gd["text"] = g ?? "";
                line = _js.Serialize(gd);
            }
            sb.Append(line);
            sb.Append("\n");
        }
        return sb.ToString();
    }

    // Rescue ONLY a command that was already durably written by the live composer. The runner
    // itself arbitrates ownership: --adopt-command is processed only after the state-dir OS lock,
    // so if another coordinator already started this process exits without touching the command.
    bool SpawnFleetAdoptCommand(string commandPath)
    {
        string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
        string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
        if (!File.Exists(py)) py = "python";
        string stateDir = Path.GetDirectoryName(_statusPath);
        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = py;
        psi.Arguments = "-m relay.fleet_runner --adopt-command \"" + commandPath + "\""
                        + " --state-dir \"" + stateDir + "\" --effort " + _effort
                        + (_fanout ? " --fanout" : " --no-fanout");
        if (_approval == "plan" || _approval == "auto") psi.Arguments += " --plan";
        psi.WorkingDirectory = repo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
        System.Diagnostics.Process.Start(psi);
        return true;
    }

    // P2 RESUME: spawn a fresh fleet with --resume (re-queues the unfinished goals from the durable
    // ledger, per fleet_runner.py). Mirrors SpawnFleet's launch construction but passes --resume
    // INSTEAD of a goals file (the runner reads .fleet/last_run_goals.json / last_run_done.json).
    bool SpawnFleetResume()
    {
        string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
        string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
        if (!File.Exists(py)) py = "python";
        string stateDir = Path.GetDirectoryName(_statusPath);

        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = py;
        psi.Arguments = "-m relay.fleet_runner --resume"
                        + " --state-dir \"" + stateDir + "\" --effort " + _effort
                        // THE "続き" DOOR NAMES ITSELF, for the same reason the "開始" door does: a
                        // resume is an ingress too, it reaches the fleet through --resume (never
                        // commands.d), and before this it recorded no admission distinct from the
                        // run. These two flags make the runner write one receipt for it, before
                        // the run lock, as route ui_resume with the submitting screen and user.
                        + " --submit-route ui_resume --submit-origin \"cockpit "
                        + Environment.UserName + " pid "
                        + System.Diagnostics.Process.GetCurrentProcess().Id + "\""
                        // RESUME NEVER CARRIED THIS AT ALL, so the setting was ignored on the
                        // resume path in BOTH directions -- off before the runner's default
                        // flipped, on after. A resumed run is the same run; it gets the same
                        // answer the operator gave, said explicitly for the reason above.
                        + (_fanout ? " --fanout" : " --no-fanout");
        psi.WorkingDirectory = repo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
        System.Diagnostics.Process.Start(psi);
        return true;
    }

    // Reimplements fleet_runner._goal_key EXACTLY: sha1 of the goal's UTF-8 bytes AFTER .strip(),
    // hex, first 16 chars. Must match byte-for-byte so the C# unfinished-count joins onto the
    // Python-written done-map. (Python str.strip() removes leading/trailing Unicode whitespace;
    // .NET String.Trim() does the same, so Trim() is the correct analog.)
    static string GoalKey(string text)
    {
        string norm = (text ?? "").Trim();
        byte[] bytes = new UTF8Encoding(false).GetBytes(norm);
        using (var sha = System.Security.Cryptography.SHA1.Create())
        {
            byte[] h = sha.ComputeHash(bytes);
            var sb = new StringBuilder(h.Length * 2);
            foreach (byte b in h) sb.Append(b.ToString("x2"));
            return sb.ToString().Substring(0, 16);
        }
    }

    // Count of goals in the last run's durable ledger that did NOT finish successfully (i.e. whose
    // key is absent from last_run_done.json with a DONE outcome). 0 when there is no ledger. Mirrors
    // fleet_runner's resume semantics: a goal is "done" only when its done-map outcome is DONE.
    int UnfinishedResumeCount()
    {
        try
        {
            string stateDir = Path.GetDirectoryName(_statusPath);
            string goalsPath = Path.Combine(stateDir, "last_run_goals.json");
            if (!File.Exists(goalsPath)) return 0;
            var ledger = _js.DeserializeObject(File.ReadAllText(goalsPath, Encoding.UTF8)) as Dictionary<string, object>;
            if (ledger == null) return 0;
            object goalsObj;
            if (!ledger.TryGetValue("goals", out goalsObj) || !(goalsObj is object[])) return 0;

            // done-map: {goal_key: outcome}; a goal counts finished only when outcome == "DONE".
            var doneKeys = new HashSet<string>();
            string donePath = Path.Combine(stateDir, "last_run_done.json");
            if (File.Exists(donePath))
            {
                var dm = _js.DeserializeObject(File.ReadAllText(donePath, Encoding.UTF8)) as Dictionary<string, object>;
                if (dm != null)
                    foreach (var kv in dm)
                        if (kv.Value != null && kv.Value.ToString() == "DONE") doneKeys.Add(kv.Key);
            }

            int n = 0;
            foreach (object o in (object[])goalsObj)
            {
                var e = o as Dictionary<string, object>;
                if (e == null) continue;
                string text = S(e, "text");
                if (string.IsNullOrEmpty(text)) continue;
                // prefer the ledger's own key (written by Python), fall back to recomputing it
                string key = S(e, "key");
                if (string.IsNullOrEmpty(key)) key = GoalKey(text);
                if (!doneKeys.Contains(key)) n++;
            }
            return n;
        }
        catch (Exception) { return 0; }
    }

    // Fingerprint the exact resume state, not just its count. Dismissing one stale banner must
    // survive restarts, but a genuinely new interrupted run (new ledger/done-map contents) must
    // become visible again. The sidecar is UI-only: it never edits the resume ledger itself.
    string ResumeStateSignature()
    {
        try
        {
            string stateDir = Path.GetDirectoryName(_statusPath);
            string goalsPath = Path.Combine(stateDir, "last_run_goals.json");
            if (!File.Exists(goalsPath)) return "";
            string donePath = Path.Combine(stateDir, "last_run_done.json");
            string payload = File.ReadAllText(goalsPath, Encoding.UTF8) + "\n\0\n"
                           + (File.Exists(donePath) ? File.ReadAllText(donePath, Encoding.UTF8) : "");
            byte[] bytes = new UTF8Encoding(false).GetBytes(payload);
            using (var sha = System.Security.Cryptography.SHA256.Create())
            {
                byte[] hash = sha.ComputeHash(bytes);
                var sb = new StringBuilder();
                foreach (byte b in hash) sb.Append(b.ToString("x2"));
                return sb.ToString().Substring(0, 24);
            }
        }
        catch (Exception) { return ""; }
    }

    bool ResumeStateDismissed(string signature)
    {
        if (string.IsNullOrEmpty(signature)) return false;
        try
        {
            if (!File.Exists(_resumeDismissPath)) return false;
            var data = _js.DeserializeObject(File.ReadAllText(_resumeDismissPath, Encoding.UTF8))
                       as Dictionary<string, object>;
            return data != null && S(data, "signature") == signature;
        }
        catch (Exception) { return false; }
    }

    void DismissResumeState(string signature)
    {
        if (string.IsNullOrEmpty(signature)) return;
        try
        {
            var data = new Dictionary<string, object>();
            data["signature"] = signature; data["dismissed_at"] = NowUnix();
            string tmp = _resumeDismissPath + ".tmp";
            File.WriteAllText(tmp, _js.Serialize(data), new UTF8Encoding(false));
            try { File.Replace(tmp, _resumeDismissPath, null); }
            catch { File.Copy(tmp, _resumeDismissPath, true); try { File.Delete(tmp); } catch { } }
        }
        catch (Exception) { }
        _lastSig = "";
        OnTick(null, null);
    }
}
