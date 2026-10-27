// FleetCockpit.Commands.cs -- sending commands to the fleet and opening workers/conversations/history entries.
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

    // ── cockpit -> fleet control channel ─────────────────────────────────────────
    // ONE COMMAND PER FILE, written through the shared FleetCommands.Write (ui/FleetCommands.cs).
    // The old ReadCommands/WriteCommands pair read all of commands.json, added this caller's key
    // and wrote the whole file back; CopilotChat.exe -- a SEPARATELY BUILT PROCESS -- did the same
    // thing to the same file, so whichever wrote second deleted the other's queued command. See
    // FleetCommands.cs for the full account. Each method below now sends ONLY its own key.
    Dictionary<string, object> Cmd1(string key, object val)
    {
        var d = new Dictionary<string, object>();
        d[key] = val;
        return d;
    }
    bool SendCommand(Dictionary<string, object> patch)
    {
        return FleetCommands.Write(_fleetDir, patch);
    }
    bool SendTrackedCommand(Dictionary<string, object> patch, out string path)
    {
        return FleetCommands.WriteTracked(_fleetDir, patch, out path);
    }

    // THE DEDUPE WENT WITH THE MERGE, DELIBERATELY. This used to read the pending `close` list
    // and skip a name already in it. With one command per file there is no pending list to
    // consult -- and reintroducing one would reintroduce the read-modify-write race that cost a
    // goal. It is acceptable here because the fleet applies closes idempotently: closing a
    // worker that is already closed is a no-op, so a duplicate close changes nothing.
    void RequestClose(string name)
    {
        var closes = new List<object>();
        closes.Add(name);
        SendCommand(Cmd1("close", closes));
    }

    void RequestSetMaxtabs(int n)
    {
        SendCommand(Cmd1("set_maxtabs", n));
    }

    // Live autoscale control: {"set_autoscale":{"on":0|1,"default":N,"max":M}}, sent as its own
    // command file through the SAME writer RequestSetMaxtabs uses, so it cannot clobber a
    // concurrent close/steer/add_goal from this window or from CopilotChat.exe.
    void RequestSetAutoscale(bool on, int def, int max)
    {
        var sa = new Dictionary<string, object>();
        sa["on"] = on ? 1 : 0;
        sa["default"] = def;
        sa["max"] = max;
        SendCommand(Cmd1("set_autoscale", sa));
    }

    // Manual priority boost: {"set_priority_boost":{"name":N,"on":0|1,"by":"cockpit","at":<unix>}}.
    // The UI shares only the FLAG NAME (priority_boost) with the fleet; the fleet owns applying it
    // (queue head, next-turn precedence, deferring non-boosted turns) AND writing the audit record
    // into .fleet (who/when/what), so the cockpit does not open a second writer onto that ledger --
    // the same one-writer discipline the close/steer/autoscale commands keep. `at` lets the fleet
    // record the request time even if it applies it a sweep later. A boost on a fan-out PARENT is
    // understood by the fleet to cover its children; the UI sends the parent name only.
    void RequestSetPriorityBoost(string name, bool on)
    {
        if (string.IsNullOrEmpty(name)) return;
        var pb = new Dictionary<string, object>();
        pb["name"] = name;
        pb["on"] = on ? 1 : 0;
        pb["by"] = "cockpit";
        pb["at"] = NowUnix();
        SendCommand(Cmd1("set_priority_boost", pb));
    }

    Dictionary<string, object> WorkerByName(string name)
    {
        if (string.IsNullOrEmpty(name)) return null;
        try
        {
            foreach (Dictionary<string, object> w in WorkersOf(_lastRoot))
                if (string.Equals(S(w, "name"), name, StringComparison.Ordinal)) return w;
            Dictionary<string, object> root = ReadStatus();
            foreach (Dictionary<string, object> w in WorkersOf(root))
                if (string.Equals(S(w, "name"), name, StringComparison.Ordinal)) return w;
        }
        catch (Exception) { }
        return null;
    }

    static bool IsLocalLoopWorker(Dictionary<string, object> w)
    {
        return w != null && string.Equals(
            S(w, "execution_profile"), "LOCAL_LOOP", StringComparison.OrdinalIgnoreCase);
    }

    bool QueueLocalLoopSteer(Dictionary<string, object> w, string text, out string failReason)
    {
        failReason = null;
        string steerFile = null;
        System.Diagnostics.Process proc = null;
        try
        {
            if (w == null) { failReason = "LOCAL_LOOP worker metadata is unavailable."; return false; }
            if (!DurableRuntimeEnabled())
            {
                failReason = _lang == 0 ? "長時間実行が無効です。" : "Durable runtime is disabled.";
                return false;
            }
            string jobId = S(w, "name").Replace("\"", "");
            string db = S(w, "local_job_db").Replace("\"", "");
            if (string.IsNullOrWhiteSpace(jobId) || string.IsNullOrWhiteSpace(db))
            {
                failReason = _lang == 0 ? "長時間タスクの識別情報が不足しています。"
                                        : "Durable task identity is incomplete.";
                return false;
            }
            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            string stateDir = Path.GetDirectoryName(_statusPath);
            string token = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString() + "_"
                         + Guid.NewGuid().ToString("N").Substring(0, 8);
            steerFile = Path.Combine(stateDir, "durable_steer_" + token + ".txt");
            File.WriteAllText(steerFile, text ?? "", new UTF8Encoding(false));

            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m relay.local_loop_controller --operator-steer-job-id \"" + jobId
                          + "\" --operator-steer-file \"" + steerFile
                          + "\" --state-dir \"" + stateDir + "\" --db \"" + db + "\"";
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            proc = System.Diagnostics.Process.Start(psi);
            if (proc == null) throw new InvalidOperationException("LOCAL_LOOP steer process did not start");
            WatchLocalLoopSteer(proc, jobId, steerFile, text ?? "");
            return true;
        }
        catch (Exception ex)
        {
            try { if (!string.IsNullOrEmpty(steerFile) && File.Exists(steerFile)) File.Delete(steerFile); }
            catch (Exception) { }
            try { if (proc != null) proc.Dispose(); } catch (Exception) { }
            failReason = (_lang == 0 ? "追加指示を保存できませんでした: " : "Could not save operator update: ") + ex.Message;
            return false;
        }
    }

    void WatchLocalLoopSteer(System.Diagnostics.Process proc, string jobId,
                             string steerFile, string text)
    {
        var timer = new System.Windows.Threading.DispatcherTimer();
        timer.Interval = TimeSpan.FromMilliseconds(200);
        timer.Tick += delegate
        {
            try
            {
                proc.Refresh();
                if (!proc.HasExited) return;
                timer.Stop();
                int code = -1;
                try { code = proc.ExitCode; } catch (Exception) { }
                if (code == 0)
                {
                    _steerDraft.Remove(jobId);
                    try { if (!string.IsNullOrEmpty(steerFile) && File.Exists(steerFile)) File.Delete(steerFile); }
                    catch (Exception) { }
                    ShowScaleToast(_lang == 0 ? "追加指示を長時間タスクへ保存しました"
                                               : "Operator update saved to the durable task");
                    _lastSig = "";
                }
                else
                {
                    _steerDraft[jobId] = text;
                    TextBox box;
                    if (_steerBoxRef.TryGetValue(jobId, out box) && box != null)
                    {
                        try { if (string.IsNullOrEmpty(box.Text)) box.Text = text; } catch (Exception) { }
                    }
                    try { if (!string.IsNullOrEmpty(steerFile) && File.Exists(steerFile)) File.Delete(steerFile); }
                    catch (Exception) { }
                    ShowScaleToast((_lang == 0 ? "追加指示を保存できませんでした。入力を復元しました。終了コード "
                                                   : "Operator update failed; input restored. Exit code ") + code);
                    _lastSig = "";
                }
                try { proc.Dispose(); } catch (Exception) { }
            }
            catch (Exception ex)
            {
                timer.Stop();
                _steerDraft[jobId] = text;
                TextBox box;
                if (_steerBoxRef.TryGetValue(jobId, out box) && box != null)
                {
                    try { if (string.IsNullOrEmpty(box.Text)) box.Text = text; } catch (Exception) { }
                }
                ShowScaleToast((_lang == 0 ? "追加指示の確認に失敗しました。入力を復元しました。 "
                                               : "Could not confirm operator update; input restored. ") + ex.Message);
                try { proc.Dispose(); } catch (Exception) { }
                _lastSig = "";
            }
        };
        timer.Start();
    }

    void RequestSteer(string name, string text)
    {
        if (string.IsNullOrEmpty(text)) return;
        var item = new Dictionary<string, object>();
        item["worker"] = name; item["text"] = text;
        var steers = new List<object>();
        steers.Add(item);
        SendCommand(Cmd1("steer", steers));
    }

    // Feature 1: the ONE steer-send code path, shared by SteerRow (expanded drawer) and
    // CollapsedSteerRow (always-visible mini box on a collapsed non-terminal card). Neither
    // caller re-implements the RequestSteer/commands.json write -- both funnel through here.
    // Returns true iff the steer was actually queued; otherwise failReason carries the reason
    // text (steer_dead) so the caller can surface it however fits its layout (inline note vs toast).
    bool TrySteerSend(string name, string text, out string failReason)
    {
        failReason = null;
        string t = (text ?? "").Trim();
        if (t.Length == 0) return false;
        Dictionary<string, object> worker = WorkerByName(name);
        if (IsLocalLoopWorker(worker))
            return QueueLocalLoopSteer(worker, t, out failReason);
        if (!RunIsLive()) { failReason = T("steer_dead"); return false; }
        RequestSteer(name, t);
        return true;
    }

    // FIX A: immediate LOCAL feedback on an open-conversation click. The main chat only notices
    // open.json on its own ~800ms poll, so without this the click reads as dead for up to 0.8s.
    // Briefly flips the clicked card's border/background to the accent colour and back; a
    // DispatcherTimer one-shot reverts it (mirrors the PaintPause optimistic-feedback pattern, and
    // the ShowScaleToast fade-timer style already used elsewhere in this file). Does NOT touch the
    // .fleet/open.json file-signal protocol -- purely a local visual cue.
    // Safe if `card` is later disposed/rebuilt mid-flash: it's just a property set on a UIElement
    // that may no longer be in the visual tree, which WPF tolerates, and the null check covers the
    // case where no card reference exists at all (e.g. a future caller passes null).
    void FlashOpen(Border card)
    {
        if (card == null) return;
        try
        {
            Brush origBorder = card.BorderBrush;
            Brush origBg = card.Background;
            card.BorderBrush = Theme.Br(Theme.Accent(_dark));
            card.Background = Theme.Br(Theme.AccentSoft(_dark));
            var t = new DispatcherTimer();
            t.Interval = TimeSpan.FromMilliseconds(350);
            t.Tick += delegate
            {
                t.Stop();
                if (card == null) return;
                card.BorderBrush = origBorder;
                card.Background = origBg;
            };
            t.Start();
        }
        catch (Exception) { }
    }

    // ① groundwork: ask the chat to open this conversation (it polls open.json).
    void OpenConv(string url)
    {
        if (string.IsNullOrEmpty(url)) return;
        try
        {
            _openSeq++;
            var o = new Dictionary<string, object>();
            o["url"] = url; o["ts"] = _openSeq;
            // The main chat is what READS open.json; if it is not running the signal lands
            // nowhere and the click does nothing. Start it (or focus it) for THIS open only --
            // before the write, so a freshly launched chat sees a recent open.json and acts on it.
            EnsureMainChatRunning();
            File.WriteAllText(_openPath, _js.Serialize(o), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    // Open a worker in the main chat by NAME (robust click target). The chat polls open.json;
    // it resolves the worker by name in status.json and shows its live snapshot, and uses url
    // for /history when a real Copilot conv_url was captured.
    void OpenWorker(string name, string url) { OpenWorker(name, url, -1); }

    // `turnIndex` is which entry the reader clicked, 0-based over the transcript's turns, or -1
    // for "just open it".
    //
    // WHY THE INDEX TRAVELS. The card lists what happened and holds no body; the full text opens
    // in the chat window. Opening it at the END would make the reader hunt for the entry they
    // had just chosen -- the review asked about this said so plainly: 「単に会話の末尾を開く
    // 設計では、対象を探し直す負担が残る」. Carrying the position is what makes the card a
    // chooser rather than a teaser.
    void OpenWorker(string name, string url, int turnIndex)
    {
        try
        {
            _openSeq++;
            var o = new Dictionary<string, object>();
            o["worker"] = name ?? ""; o["url"] = url ?? ""; o["ts"] = _openSeq;
            // Written only when there is one, so a reader of open.json can tell "no position
            // was asked for" from "position 0" -- which are different requests.
            if (turnIndex >= 0) o["turn_index"] = turnIndex;
            EnsureMainChatRunning();
            File.WriteAllText(_openPath, _js.Serialize(o), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    // Open a HISTORY (archived) conversation. Carries the exact disk transcript path so the chat
    // shows the full body straight from the .jsonl even when conv_url is empty -- plus the worker
    // name as a fallback (the chat resolves the newest .jsonl by name). Fixes the completed-
    // conversation "本文はまだ取得できません" dead-end.
    void OpenHistory(string name, string url, string transcript)
    {
        try
        {
            _openSeq++;
            var o = new Dictionary<string, object>();
            o["worker"] = name ?? ""; o["url"] = url ?? "";
            o["transcript"] = transcript ?? ""; o["ts"] = _openSeq;
            EnsureMainChatRunning();
            File.WriteAllText(_openPath, _js.Serialize(o), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    // Launch the main chat (CopilotChat.exe) if it is not already running; otherwise bring it
    // to the front. Shared by OpenMain() and by the card Open handlers above, so pressing Open
    // on a card STARTS the main window when it is absent instead of writing a signal nobody
    // reads. This opens ONLY the main chat (the surface a card Open targets) -- it does not
    // touch the cockpit or any other window, so the earlier "every button opened everything"
    // behaviour does not return.
    void EnsureMainChatRunning()
    {
        try
        {
            var existing = System.Diagnostics.Process.GetProcessesByName("CopilotChat");
            if (existing.Length > 0)
            {
                try { if (existing[0].MainWindowHandle != IntPtr.Zero) SetForegroundWindow(existing[0].MainWindowHandle); } catch { }
                return;
            }
            string exe = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "CopilotChat.exe");
            if (File.Exists(exe)) System.Diagnostics.Process.Start(exe);
        }
        catch { }
    }
}
