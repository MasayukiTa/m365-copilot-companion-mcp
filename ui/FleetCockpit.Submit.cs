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

    static string TasksDir()
    {
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;          // ...\ui\
        return Path.GetFullPath(Path.Combine(exeDir, "..", ".fleet", "tasks"));
    }

    // What the "submitted" group shows right now: the merged list, newest first, as of the
    // last tick. The one read EmptyState, the rows and PublishHealthStrip all share.
    List<SubmittedView> ReadQueuedJobs()
    {
        List<SubmittedView> now = _submittedNow;
        return now ?? new List<SubmittedView>();
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

    void NoteSubmitted(IEnumerable<string> goals)
    {
        NoteSubmitted(goals, CaptureSubmissionBaseline());
    }

    void NoteSubmitted(IEnumerable<string> goals, SubmissionBaseline baseline)
    {
        try
        {
            SubmissionBaseline b = baseline ?? CaptureSubmissionBaseline();
            foreach (string g in goals)
                _submitted.AddLocal(SubmittedTasks.GoalTextOf(g), b.CapturedUnix,
                                    b.Started, b.Workers, b.History);
            Dispatcher.BeginInvoke(new Action(ForceRender));
        }
        catch (Exception) { }
    }

    void StartFleet()
    {
        try
        {
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
        SubmissionBaseline submitBaseline = CaptureSubmissionBaseline();

        // This method is entered from the ACTIVE composer. Do not re-decide ownership from one
        // status read here: a transient unreadable/stale snapshot used to divert this submission
        // into StartFleet(), where the state-dir lock correctly rejected the second coordinator
        // after the UI had already cleared the input. Always land the tracked command; its applied
        // receipt + WatchLiveAddHandoff resolve whether the old run consumes it or a new run adopts it.

        var adds = new List<object>();
        foreach (string goal in goals)
        {
            var item = new Dictionary<string, object>();
            item["text"] = goal;
            item["priority"] = false;
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
        timer.Tick += delegate
        {
            try
            {
                string receiptError;
                int receiptState = LiveAddReceiptState(ackPath, out receiptError);
                if (receiptState > 0) { timer.Stop(); return; }
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
                if ((DateTime.UtcNow - lastRescue).TotalSeconds < 2.0) return;
                lastRescue = DateTime.UtcNow;
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
                    bool waitForClosingRun = false, string submittedText = null)
    {
        foreach (string rawGoal in (goals ?? new List<string>()))
        {
            if (!IsLocalLoopControlGoal(SubmittedTasks.GoalTextOf(rawGoal))) continue;
            if (_startNote != null) _startNote.Text = _lang == 0
                ? "LOCAL_LOOP の内部制御文は通常Fleetとして起動できません。耐久タスクのカードから再開してください。"
                : "LOCAL_LOOP control text cannot start ordinary Fleet work. Resume the durable task card instead.";
            return false;
        }
        SubmissionBaseline submitBaseline = CaptureSubmissionBaseline();
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
        if (waitForClosingRun) psi.Arguments += " --wait-for-state-dir-seconds 60";
        // BOTH VALUES ARE SAID OUT LOUD. This used to append "--fanout" when on and nothing
        // when off -- and once the runner's default became true, saying nothing meant ON, so
        // a person who typed /fanout off got fan-out anyway while the cockpit reported OFF.
        // A boolean expressed by the presence of a flag can only state one of its two values.
        psi.Arguments += _fanout ? " --fanout" : " --no-fanout";
        psi.WorkingDirectory = repo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
        System.Diagnostics.Process proc = System.Diagnostics.Process.Start(psi);
        if (proc == null) return false;
        // On top of the list NOW, before the new run's first snapshot exists. Every spawn path
        // comes through here: the composer's StartFleet, a retry or bulk retry with no live run,
        // and the continue flows (whose lines are {"text":..} objects -- GoalTextOf reads them).
        NoteSubmitted(goals, submitBaseline);
        if (waitForClosingRun)
        {
            _fleetLaunchPending = true;
            WatchFreshFleetLaunch(proc, submitBaseline, goalsFile, submittedText ?? "",
                                  goals, planMode);
        }
        else
        {
            try { proc.Dispose(); } catch (Exception) { }
        }
        return true;
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

    void WatchFreshFleetLaunch(System.Diagnostics.Process proc, SubmissionBaseline baseline,
                               string goalsFile, string submittedText, List<string> goals, bool planMode)
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
                    if (_goalInput.Text == submittedText) _goalInput.Text = "";
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
                if (!proc.HasExited) return;
                if (exitSeenAt == DateTime.MinValue)
                {
                    exitSeenAt = DateTime.UtcNow;
                    return;
                }
                if ((DateTime.UtcNow - exitSeenAt).TotalSeconds < 2.0) return;

                timer.Stop();
                _fleetLaunchPending = false;
                int code = -1;
                try { code = proc.ExitCode; } catch (Exception) { }
                try { if (File.Exists(goalsFile)) File.Delete(goalsFile); } catch (Exception) { }
                if (_startNote != null)
                    _startNote.Text = (_lang == 0
                        ? "新しい実行を開始できませんでした。入力は残しています。終了コード "
                        : "Could not start the new run; your input was kept. Exit code ")
                        + code;
                try { proc.Dispose(); } catch (Exception) { }
            }
            catch (Exception ex)
            {
                timer.Stop();
                _fleetLaunchPending = false;
                if (_startNote != null)
                    _startNote.Text = (_lang == 0
                        ? "新しい実行の開始確認に失敗しました。入力は残しています。 "
                        : "Could not confirm the new run start; your input was kept. ")
                        + ex.Message;
                try { proc.Dispose(); } catch (Exception) { }
            }
        };
        timer.Start();
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
