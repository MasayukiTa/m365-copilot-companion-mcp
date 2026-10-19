// CopilotChat.FleetStatus.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Text;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Documents;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using System.Web.Script.Serialization;

partial class ChatWindow
{
    // True while a fleet run is actively driving the companion Edge: status.json says
    // running AND it was updated recently (the runner rewrites it ~1/s). When this holds we

    // must NOT call /switch+/history -- doing so would PAGE.goto the shared companion Edge
    // onto this conversation and clobber the live send. Stale/!running -> safe to scrape.
    bool FleetRunningFresh()
    {
        try
        {
            string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(sp)) return false;
            string txt;
            using (var fsr = new FileStream(sp, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8)) txt = sr.ReadToEnd();
            var d = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (d == null) return false;
            bool running = d.ContainsKey("running") && d["running"] != null && Convert.ToBoolean(d["running"]);
            if (!running) return false;
            double updated = (d.ContainsKey("updated") && d["updated"] != null) ? Convert.ToDouble(d["updated"]) : 0;
            double now = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;
            return (now - updated) <= 20.0;   // fresh within 20s -> the runner is live
        }
        catch { return false; }
    }

    // Build a readable live snapshot from a status.json worker dict: goal/status/outcome,
    // turn N/max, plan steps, latest response, verification state, and reason. Mirrors the
    // cockpit card so the main chat shows progress when /history can't be scraped.
    void RenderFleetSnapshot(Dictionary<string, object> w)
    {
        AddUser(SS(w, "goal"));
        AddAssistant(BuildFleetStatusTail(w, includeLast: true));
    }

    // The status/turn/plan/verify/reason block for a worker, as a string. When includeLast
    // is false the "latest response" (the truncated `last`) is omitted -- used when the full
    // transcript is already shown above, so we don't duplicate the last turn in truncated form.
    string BuildFleetStatusTail(Dictionary<string, object> w, bool includeLast)
    {
        bool ja = _lang == 0;
        var sb = new StringBuilder();
        string status = SS(w, "status");
        string outcome = SS(w, "outcome");
        string head = string.IsNullOrEmpty(outcome) ? status : (status + " / " + outcome);
        sb.Append(ja ? "状態: " : "Status: ").Append(string.IsNullOrEmpty(head) ? (ja ? "(不明)" : "(unknown)") : head);
        string turn = SS(w, "turn"); string maxt = SS(w, "max_turns");
        if (!string.IsNullOrEmpty(turn))
        {
            sb.Append(ja ? "　ターン " : "   Turn ").Append(turn);
            if (!string.IsNullOrEmpty(maxt)) sb.Append("/").Append(maxt);
        }
        sb.Append('\n');

        // plan steps
        if (w.ContainsKey("plan") && w["plan"] is object[])
        {
            var steps = (object[])w["plan"];
            if (steps.Length > 0)
            {
                sb.Append('\n').Append(ja ? "計画:" : "Plan:").Append('\n');
                int n = 1;
                foreach (object s in steps)
                {
                    if (s == null) continue;
                    sb.Append("  ").Append(n).Append(". ").Append(s.ToString()).Append('\n');
                    n++;
                }
            }
        }

        // latest scraped response (skipped when the full transcript is already rendered above)
        string last = SS(w, "last");
        if (includeLast && !string.IsNullOrEmpty(last))
            sb.Append('\n').Append(ja ? "最新の応答:" : "Latest response:").Append('\n').Append(last).Append('\n');

        // verification state
        string verified = SS(w, "verified");
        string vattempts = SS(w, "verify_attempts");
        if (!string.IsNullOrEmpty(verified) || !string.IsNullOrEmpty(vattempts))
        {
            string vstr;
            if (verified == "True") vstr = ja ? "検証OK" : "verified";
            else if (verified == "False") vstr = ja ? "未検証" : "not verified";
            else vstr = ja ? "検証中/未判定" : "pending";
            sb.Append('\n').Append(ja ? "検証: " : "Verification: ").Append(vstr);
            if (!string.IsNullOrEmpty(vattempts)) sb.Append(ja ? "（試行 " : " (attempts ").Append(vattempts).Append(ja ? "）" : ")");
            sb.Append('\n');
        }

        // reason
        string reason = SS(w, "reason");
        if (!string.IsNullOrEmpty(reason))
            sb.Append('\n').Append(ja ? "理由: " : "Reason: ").Append(reason).Append('\n');

        return sb.ToString().TrimEnd('\n');
    }

    // Re-render the live fleet snapshot in place (called from the poll timer when
    // status.json changes and a fleet snapshot is the active view). Cheap: only touches
    // the message panel when the matching worker is still present.
    void RefreshFleetSnapshot()
    {
        if (string.IsNullOrEmpty(_activeFleetUrl)) return;
        if (_conv == null || _conv.ConvUrl != _activeFleetUrl) return;   // user navigated away
        var w = ReadFleetWorker(_activeFleetUrl);
        if (w == null) return;

        // REBUILD ONLY WHEN WHAT IS ON SCREEN WOULD DIFFER.
        //
        // The trigger for this method is status.json's MTIME, and a live fleet rewrites that
        // file about once a second whether or not anything a reader can see has changed. So the
        // whole message panel was cleared and re-created every second: `_messages.Children
        // .Clear()` destroys the TextBox the user is selecting in, and a selection cannot
        // survive the control it lives in. Select a few words, and they were gone before Ctrl+C
        // -- "選択してctrl cしてもなにもコピーできていない", and nothing was wrong with the
        // controls, which are read-only and selectable and always were.
        //
        // The signature is the RENDERED CONTENT -- the transcript lines and the status tail --
        // and not the worker dict, which carries per-second fields that would defeat the point.
        string latestTranscript = SS(w, "transcript");
        _conv.Transcripts = FleetConvIdentity.MergeTranscriptLineage(_conv.Transcripts, null, latestTranscript);
        _conv.Transcript = FleetConvIdentity.LatestTranscript(_conv.Transcripts, latestTranscript);
        var txPre = ReadTranscriptLineage(_conv.Transcripts);
        string tailPre = BuildFleetStatusTail(w, includeLast: txPre.Count == 0);

        // THE BAND IS UPDATED FIRST AND UNCONDITIONALLY, because it is the part that moves. It
        // is not in _messages, so writing it cannot disturb a selection or a scroll position --
        // which is the whole reason the status was taken out of the transcript.
        ShowRunState(tailPre);

        // AND THE TRANSCRIPT IS SIGNED WITHOUT IT -- per turn, not as one string. Including the
        // status would put the conversation back on the status's clock: every tick would differ,
        // the guard would never hold, and the rebuild would return exactly as often as before.
        var sigs = new List<string>();
        foreach (var m in txPre) sigs.Add(m.Role + "" + m.Text);

        // APPEND WHAT IS NEW; REBUILD ONLY WHEN THE PAST CHANGED.
        //
        // The signature guard above stops the rebuild on a tick where nothing moved, which is
        // most of them -- but a live worker DOES produce turns, and on each of those the whole
        // panel was still being cleared and re-created. The review that asked for this said so
        // in as many words: 「表示署名の判定だけでは、本文が増えた際の全再構築が残る。既存部分
        // を保持し、追記と対象箇所の更新で済むことを設計要件にします」. A reader mid-selection
        // when the agent answers loses the selection for the same reason as before, just less
        // often -- and "less often" is not a property anybody can rely on.
        //
        // The past does not normally change: a transcript is append-only. So compare turn by
        // turn, and when everything already rendered is still identical, add only the tail. A
        // rebuild stays the answer for the case that is NOT an append -- a different worker, a
        // recycled conversation, a transcript rewritten underneath us -- because then what is on
        // screen is about something else.
        int common = 0;
        while (common < sigs.Count && common < _fleetTurnSigs.Count
               && sigs[common] == _fleetTurnSigs[common]) common++;
        bool append = common == _fleetTurnSigs.Count && common > 0 && sigs.Count > common
                      && _messages.Children.Count > 0;
        if (append)
        {
            for (int i = common; i < sigs.Count; i++)
            {
                var m = txPre[i];
                if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text);
            }
            _fleetTurnSigs = sigs;
            return;
        }
        if (sigs.Count == _fleetTurnSigs.Count && common == sigs.Count) return;
        _fleetTurnSigs = sigs;

        _messages.Children.Clear();
        var note = new TextBlock { Text = T("fleetview_note"), TextWrapping = TextWrapping.Wrap, FontSize = 12.5, Margin = new Thickness(2, 2, 2, 12) };
        SetRef(note, TextBlock.ForegroundProperty, "Muted");
        _messages.Children.Add(note);
        // If this worker has a persisted transcript, re-render the WHOLE conversation from disk
        // (untruncated) and append the live status tail -- otherwise fall back to the snapshot
        // fragment. Reading the jsonl touches only disk, never the live companion Edge.
        // The same read the signature was taken from -- reading the transcript twice would put
        // a second disk hit on a path that runs whenever the fleet writes.
        var tx = txPre;
        if (tx.Count > 0)
        {
            // NO STATUS TURN AT THE END. It used to be appended here as an assistant message;
            // it is in the band above now.
            foreach (var m in tx) { if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text); }
        }
        else
        {
            RenderFleetSnapshot(w);
        }
        StickToEnd();
    }

    // Put the run's current state in the band, or hide it. Touches nothing in the transcript.
    void ShowRunState(string text)
    {
        if (_statusBand == null || _statusText == null) return;
        string t = (text ?? "").Trim();
        if (t.Length == 0) { _statusBand.Visibility = Visibility.Collapsed; return; }
        if (!string.Equals(_statusText.Text, t, StringComparison.Ordinal)) _statusText.Text = t;
        _statusBand.Visibility = Visibility.Visible;
    }

    // If a fleet snapshot is the active view, re-render it when status.json's mtime changes
    // so the user sees progress update live in the main chat (mirrors the cockpit refresh).
    void CheckFleetSnapshot()
    {
        try
        {
            if (string.IsNullOrEmpty(_activeFleetUrl)) return;
            if (_conv == null || _conv.ConvUrl != _activeFleetUrl) { _activeFleetUrl = null; ShowRunState(null); RefreshSteerVisual(); return; }
            string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(sp)) return;
            long m = File.GetLastWriteTimeUtc(sp).Ticks;
            if (m == _statusMtime) return;
            _statusMtime = m;
            RefreshFleetSnapshot();
        }
        catch { }
    }

    // Read status.json and count ACTIVE fleet workers -- status neither terminal nor "pending",
    // by ChatSend.IsTerminalWorkerStatus, the one list the steer lookup uses too. Anything else
    // (e.g. "running", "verifying", "planning") is actively working. 0 when status.json is absent.
    int ReadActiveFleetWorkerCount()
    {
        try { return ChatSend.ActiveWorkerCount(Path.Combine(Path.GetDirectoryName(_convsPath), "status.json")); }
        catch { return 0; }
    }

    // Update the Fleet chip in the main header. Called on the 800ms poll timer.
    // Shows "Fleet: N" (collapsed when N == 0). Clicking opens the cockpit.
    void RefreshFleetChip()
    {
        if (_fleetChip == null || _fleetChipLabel == null) return;
        try
        {
            int n = ReadActiveFleetWorkerCount();
            if (n > 0)
            {
                _fleetChipLabel.Text = "Fleet: " + n;
                _fleetChip.Visibility = Visibility.Visible;
            }
            else
            {
                _fleetChip.Visibility = Visibility.Collapsed;
            }
        }
        catch { }
    }

    // ── Fleet strip (compact one-liner above the composer) ────────────────────
    // A single ~28px-tall band showing fleet state inline. Collapses when no
    // workers exist or when the run completed more than 30 min ago.
    UIElement BuildFleetStrip()
    {
        _fleetStripBody = new StackPanel { Margin = new Thickness(0, 0, 0, 0) };

        _fleetStrip = new Border
        {
            Child = _fleetStripBody,
            CornerRadius = new CornerRadius(Theme.RadCard),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(12, 4, 12, 4),
            Margin = new Thickness(0, 0, 0, 6),
            Visibility = Visibility.Collapsed
        };
        SetRef(_fleetStrip, BackgroundProperty, "PanelAlt");
        SetRef(_fleetStrip, Border.BorderBrushProperty, "Border");
        return _fleetStrip;
    }

    // Compute a lightweight signature from status.json so we only rebuild the strip's
    // DOM when something actually changed (avoids per-tick flicker / GC pressure).
    // Signature: "<running>|<updated>|<run_label>|<goal_count>|<w0key:w0status>|..."
    string FleetStripSignature(Dictionary<string, object> statusDoc)
    {
        if (statusDoc == null) return "";
        var sb = new StringBuilder();
        sb.Append(SS(statusDoc, "running")).Append('|');
        sb.Append(SS(statusDoc, "updated")).Append('|');
        sb.Append(SS(statusDoc, "run_label")).Append('|');
        sb.Append(SS(statusDoc, "goal_count")).Append('|');
        if (statusDoc.ContainsKey("workers") && statusDoc["workers"] is object[])
        {
            foreach (object o in (object[])statusDoc["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                sb.Append(SS(w, "name")).Append(':').Append(SS(w, "status")).Append('|');
            }
        }
        return sb.ToString();
    }

    // Refresh the fleet strip from status.json. Called on the 800ms timer tick.
    // Rebuilds the inner DOM only when the signature (running/updated/worker statuses) changes.
    void RefreshFleetStrip()
    {
        if (_fleetStrip == null || _fleetStripBody == null) return;
        try
        {
            string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(sp))
            {
                if (_fleetStrip.Visibility != Visibility.Collapsed)
                {
                    _fleetStrip.Visibility = Visibility.Collapsed;
                    _fleetStripSig = null;
                }
                return;
            }
            string txt;
            using (var fsr = new FileStream(sp, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8)) txt = sr.ReadToEnd();
            var doc = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (doc == null)
            {
                if (_fleetStrip.Visibility != Visibility.Collapsed) { _fleetStrip.Visibility = Visibility.Collapsed; _fleetStripSig = null; }
                return;
            }

            // Collect workers array
            object[] workers = null;
            if (doc.ContainsKey("workers") && doc["workers"] is object[])
                workers = (object[])doc["workers"];

            // No workers -> collapse and return
            if (workers == null || workers.Length == 0)
            {
                if (_fleetStrip.Visibility != Visibility.Collapsed) { _fleetStrip.Visibility = Visibility.Collapsed; _fleetStripSig = null; }
                return;
            }

            // ── Compute summary values ──────────────────────────────────────────
            bool running = doc.ContainsKey("running") && doc["running"] != null && Convert.ToBoolean(doc["running"]);
            double updated = (doc.ContainsKey("updated") && doc["updated"] != null) ? Convert.ToDouble(doc["updated"]) : 0;
            double nowEpoch = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
            double ageSec = (updated > 0) ? (nowEpoch - updated) : -1;

            // Auto-hide stale completed runs — check BEFORE signature gate so it
            // fires every tick even when status.json has not changed.
            if (!running && ageSec > 1800)
            {
                if (_fleetStrip.Visibility != Visibility.Collapsed)
                {
                    _fleetStrip.Visibility = Visibility.Collapsed;
                    _fleetStripSig = null;
                }
                return;
            }

            // Check signature -- skip rebuild if nothing changed
            string sig = FleetStripSignature(doc);
            if (sig == _fleetStripSig && _fleetStrip.Visibility == Visibility.Visible) return;
            _fleetStripSig = sig;

            // run_label / goal_count from top-level fields (new schema)
            string runLabel = SS(doc, "run_label");
            string goalCountStr = SS(doc, "goal_count");

            // Fallback label: first worker's goal, truncated
            if (string.IsNullOrEmpty(runLabel) && workers.Length > 0)
            {
                var fw = workers[0] as Dictionary<string, object>;
                if (fw != null)
                {
                    string fg = SS(fw, "goal");
                    if (!string.IsNullOrEmpty(fg))
                    {
                        // use first non-empty line
                        string[] fgLines = fg.Replace("\r", "").Split('\n');
                        foreach (string fgl in fgLines)
                        {
                            string trimmed = fgl.Trim();
                            if (trimmed.Length > 0) { runLabel = trimmed; break; }
                        }
                    }
                }
            }
            // Truncate to ~50 chars
            if (!string.IsNullOrEmpty(runLabel) && runLabel.Length > 50)
                runLabel = runLabel.Substring(0, 50) + "…";

            // Worker counts
            int doneCount2 = 0, attentionCount2 = 0, runningCount2 = 0, pendingCount2 = 0;
            foreach (object o in workers)
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                string st = SS(w, "status").ToLowerInvariant();
                if (st == "done" || st == "resolved" || st == "cancelled" || st == "freed") doneCount2++;
                else if (st == "stuck" || st == "maxturns" || st == "error" || st == "awaiting") attentionCount2++;
                else if (st == "pending") pendingCount2++;
                else runningCount2++;
            }
            // Freshness string (shared by both branches)
            string freshStr = "";
            if (ageSec >= 0)
            {
                string ageStr;
                if (ageSec < 60) ageStr = ((int)ageSec) + (_lang == 0 ? "秒前" : "s ago");
                else if (ageSec < 3600) ageStr = ((int)(ageSec / 60)) + (_lang == 0 ? "分前" : "m ago");
                else ageStr = ((int)(ageSec / 3600)) + (_lang == 0 ? "時間前" : "h ago");
                freshStr = _lang == 0 ? ("最終更新 " + ageStr) : ("updated " + ageStr);
            }

            // ── Rebuild inner DOM — single compact row ──────────────────────────
            bool allDone = !running;
            bool ja = _lang == 0;

            _fleetStripBody.Children.Clear();

            // Determine status dot color. pending (queued / not yet started) must NOT read as
            // Success green — that hides the most anxious state of a long delegation ("not started
            // yet"). When nothing is running but work is queued, use Info (blue/neutral).
            string dotColor;
            if (runningCount2 > 0)
                dotColor = Theme.Info(_dark);
            else if (attentionCount2 > 0)
                dotColor = Theme.Warning(_dark);
            else if (pendingCount2 > 0)
                dotColor = Theme.Info(_dark);
            else
                dotColor = Theme.Success(_dark);

            // Build status summary text. Only show "done" (Success) once nothing is running AND
            // nothing is pending; while queued show "待機中 N" / "Queued N".
            string summaryText;
            if (runningCount2 > 0)
                summaryText = ja ? ("実行中 " + runningCount2) : (runningCount2 + " running");
            else if (pendingCount2 > 0)
                summaryText = ja ? ("待機中 " + pendingCount2) : ("Queued " + pendingCount2);
            else
                summaryText = ja ? (doneCount2 + "件完了") : (doneCount2 + " done");

            // Build tooltip text (run_label + goal_count) — replaces the old label line
            string tipText = runLabel;
            if (!string.IsNullOrEmpty(tipText) && !string.IsNullOrEmpty(goalCountStr))
                tipText = tipText + (ja ? " ・" : " ·") + goalCountStr + (ja ? "件" : " goals");

            // ── Single DockPanel row ────────────────────────────────────────────
            var row = new DockPanel { Height = 26, VerticalAlignment = VerticalAlignment.Center };
            if (!string.IsNullOrEmpty(tipText)) row.ToolTip = tipText;

            // RIGHT: compact open button
            var openBtn = new Button
            {
                Content = ja ? "開く" : "Open",
                FontSize = 11,
                Height = 20,
                Padding = new Thickness(8, 0, 8, 0),
                Cursor = Cursors.Hand,
                BorderThickness = new Thickness(1),
                VerticalAlignment = VerticalAlignment.Center,
                FontWeight = FontWeights.Normal
            };
            SetRef(openBtn, BackgroundProperty, "PanelAlt");
            SetRef(openBtn, ForegroundProperty, "Accent");
            SetRef(openBtn, Control.BorderBrushProperty, "Border");
            openBtn.Click += delegate { OpenCockpit(); };
            DockPanel.SetDock(openBtn, Dock.Right);
            row.Children.Add(openBtn);

            // LEFT: status dot + "フリート" label + summary + freshness
            var leftStack = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                VerticalAlignment = VerticalAlignment.Center
            };

            // Status dot (8px circle)
            var dot = new Border
            {
                Width = 8, Height = 8,
                CornerRadius = new CornerRadius(Theme.RadChip),
                Margin = new Thickness(0, 0, 5, 0),
                VerticalAlignment = VerticalAlignment.Center
            };
            dot.Background = new SolidColorBrush(Theme.Col(dotColor));
            leftStack.Children.Add(dot);

            // "フリート" prefix
            var fleetLabel = new TextBlock
            {
                // 言語トグルで切り替わっていなかった。英語表示でも日本語のまま出ていた。
                Text = (_lang == 0 ? "フリート" : "Fleet"),
                FontSize = 11,
                FontWeight = FontWeights.SemiBold,
                VerticalAlignment = VerticalAlignment.Center
            };
            SetRef(fleetLabel, TextBlock.ForegroundProperty, "Muted");
            leftStack.Children.Add(fleetLabel);

            // Show a short truncated run label inline for delegation context. Shown while RUNNING
            // and for the completed window too: the strip itself auto-hides after 30 min of being
            // not-running (see the ageSec > 1800 guard above), so any non-running strip that reaches
            // here is within the completed-30-min window — no extra age check needed here.
            if (!string.IsNullOrEmpty(runLabel))
            {
                string shortLabel = runLabel.Length > 20 ? runLabel.Substring(0, 20) + "…" : runLabel;
                var sepRL = new TextBlock
                {
                    Text = " · ",
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center
                };
                SetRef(sepRL, TextBlock.ForegroundProperty, "Faint");
                leftStack.Children.Add(sepRL);

                var runLabelTb = new TextBlock
                {
                    Text = shortLabel,
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center
                };
                SetRef(runLabelTb, TextBlock.ForegroundProperty, "Muted");
                leftStack.Children.Add(runLabelTb);
            }

            // separator dot
            var sep1 = new TextBlock
            {
                Text = " · ",
                FontSize = 11,
                VerticalAlignment = VerticalAlignment.Center
            };
            SetRef(sep1, TextBlock.ForegroundProperty, "Faint");
            leftStack.Children.Add(sep1);

            // Status summary text
            var summaryTb = new TextBlock
            {
                Text = summaryText,
                FontSize = 11,
                VerticalAlignment = VerticalAlignment.Center
            };
            SetRef(summaryTb, TextBlock.ForegroundProperty, "Muted");
            leftStack.Children.Add(summaryTb);

            // Attention segment (Warning color when >0)
            if (attentionCount2 > 0)
            {
                var sep2 = new TextBlock
                {
                    Text = " · ",
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center
                };
                SetRef(sep2, TextBlock.ForegroundProperty, "Faint");
                leftStack.Children.Add(sep2);

                string attText = ja ? ("要対応 " + attentionCount2) : (attentionCount2 + " attention");
                var attTb = new TextBlock
                {
                    Text = attText,
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center,
                    Foreground = new SolidColorBrush(Theme.Col(Theme.Warning(_dark)))
                };
                leftStack.Children.Add(attTb);
            }

            // Freshness
            if (!string.IsNullOrEmpty(freshStr))
            {
                var sep3 = new TextBlock
                {
                    Text = " · ",
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center
                };
                SetRef(sep3, TextBlock.ForegroundProperty, "Faint");
                leftStack.Children.Add(sep3);

                var freshTb = new TextBlock
                {
                    Text = freshStr,
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center
                };
                SetRef(freshTb, TextBlock.ForegroundProperty, "Faint");
                leftStack.Children.Add(freshTb);
            }

            row.Children.Add(leftStack);
            _fleetStripBody.Children.Add(row);
            _fleetStrip.Visibility = Visibility.Visible;
        }
        catch { }
    }
}
