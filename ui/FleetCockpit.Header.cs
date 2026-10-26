// FleetCockpit.Header.cs -- the header, rate strip, quota meter and severity colours.
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

    // ---- stale/health verdict (liveness from the runner process, not status.json age) ----
    // Runner identity = fleet_run_active.json pid + pid_birth (process create time, ms since epoch,
    // written by relay/fleet_runner.py _pid_birth_token). Alive = the pid exists AND its start time
    // matches pid_birth (a recycled pid has a different start time). state: 1 alive, 0 dead,
    // -1 cannot tell. Cached 3 s together with the newest top-level state-dir file write time.
    DateTime _rlAt = DateTime.MinValue; int _rlState = -1; double _rlActivityAge = -1;
    void RunnerLiveness(out int state, out double activityAgeS)
    {
        if ((DateTime.UtcNow - _rlAt).TotalSeconds < 3.0) { state = _rlState; activityAgeS = _rlActivityAge; return; }
        int st = -1; double act = -1;
        try
        {
            string dir = Path.GetDirectoryName(ResolvePath(null));
            string marker = Path.Combine(dir, "fleet_run_active.json");
            if (File.Exists(marker))
            {
                var m = _js.DeserializeObject(File.ReadAllText(marker, Encoding.UTF8)) as Dictionary<string, object>;
                object pidObj;
                if (m != null && m.TryGetValue("pid", out pidObj) && pidObj != null)
                {
                    int pid = Convert.ToInt32(pidObj);
                    long birth = 0; object bo;
                    if (m.TryGetValue("pid_birth", out bo) && bo != null) birth = Convert.ToInt64(bo);
                    try
                    {
                        var proc = System.Diagnostics.Process.GetProcessById(pid);
                        st = 1;
                        if (birth > 0)
                        {
                            long cur = (long)(proc.StartTime.ToUniversalTime()
                                       - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalMilliseconds;
                            if (Math.Abs(cur - birth) > 3000) st = 0;   // pid recycled by another process
                        }
                    }
                    catch (ArgumentException) { st = 0; }
                    catch (Exception) { st = -1; }
                }
            }
            DateTime newest = DateTime.MinValue;
            foreach (string f in Directory.GetFiles(dir))
            {
                DateTime t = File.GetLastWriteTimeUtc(f);
                if (t > newest) newest = t;
            }
            if (newest > DateTime.MinValue) act = (DateTime.UtcNow - newest).TotalSeconds;
        }
        catch (Exception) { }
        _rlAt = DateTime.UtcNow; _rlState = st; _rlActivityAge = act;
        state = st; activityAgeS = act;
    }

    // kind: "" (show nothing) or a Pill style. The text names the runner's real state.
    void StaleVerdict(Dictionary<string, object> root, bool running, double updated,
                      out string kind, out string text)
    {
        const double STALE_SOFT = 45.0;
        const double STALE_HARD = 90.0;
        kind = ""; text = "";
        if (!running || updated <= 0) return;
        double age = NowUnix() - updated;
        if (age <= STALE_SOFT) return;
        bool ja = _lang == 0;
        int st; double act;
        RunnerLiveness(out st, out act);
        if (st == 0)
        {
            kind = "danger";
            text = ja ? "ランナーのプロセスが存在しません（停止）" : "runner process is gone (stopped)";
            return;
        }
        if (st == 1)
        {
            // Alive: say what the sweep is waiting for when workers carry a reason.
            var counts = new Dictionary<string, int>();
            object wo;
            if (root.TryGetValue("workers", out wo) && wo is object[])
                foreach (object ow in (object[])wo)
                {
                    var w = ow as Dictionary<string, object>;
                    if (w == null || IsTerminalWorker(w)) continue;
                    string r = S(w, "reason");
                    if (string.IsNullOrEmpty(r)) continue;
                    int c; counts.TryGetValue(r, out c); counts[r] = c + 1;
                }
            string top = ""; int best = 0;
            foreach (var kv in counts) if (kv.Value > best) { best = kv.Value; top = kv.Key; }
            if (top.Length > 70) top = top.Substring(0, 70) + "…";
            string tail = top != "" ? ": " + top : "";
            kind = (age > STALE_HARD && act > STALE_HARD) ? "warning" : "info";
            text = ja ? "ランナー稼働中・スイープ処理中 " + (int)age + "秒" + tail
                      : "runner alive, sweep busy " + (int)age + "s" + tail;
            return;
        }
        // Process state unknown: old wording, but recent file activity vetoes the hard claim.
        if (age > STALE_HARD && !(act >= 0 && act < STALE_HARD)) { kind = "danger"; text = T("stale"); }
        else { kind = "warning"; text = T("stale_wait"); }
    }

    void UpdateHeader(Dictionary<string, object> root)
    {
        int total = I(root, "total");
        int done = I(root, "done_count");
        bool running = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
        double started = Dbl(root, "started");
        double updated = Dbl(root, "updated");
        double elapsed = running ? (started > 0 ? NowUnix() - started : 0)
                                 : (root.ContainsKey("elapsed_s") ? Dbl(root, "elapsed_s")
                                    : (updated > 0 && started > 0 ? updated - started : 0));

        // Spec: header is always "Fleet" (product surface name, both languages).
        _header.Text = "Fleet";

        // Compute running/queued/done counts from the workers array.
        // TRUTHFUL PER-STATE COUNTS. `else cntRunning++` used to lump every non-terminal,
        // non-pending worker into "running": a run with 3 streaming (waiting) and 62 ready
        // workers read "65 running" although max_concurrent was 2. A worker is RUNNING only while
        // a turn is actually in flight (STATUS_PILL "実行中"/検証中/反証中/外部調査中/新規会話);
        // `ready` (準備: tab prepared, waiting for a concurrency slot) and approval gates are
        // counted on their own, and `pending` stays the queue.
        int cntRunning = 0, cntQueued = 0, cntDoneW = 0, cntIntr = 0, cntReady = 0, cntGate = 0;
        object wo2;
        if (root.TryGetValue("workers", out wo2) && wo2 is object[])
        {
            foreach (object ow in (object[])wo2)
            {
                var ww = ow as Dictionary<string, object>;
                if (ww == null) continue;
                string wst = S(ww, "status");
                if (IsTerminalWorker(ww)) cntDoneW++;
                else if (IsInterruptedWorker(ww)) cntIntr++;   // not running, not done, not a failure
                else if (wst == "pending") cntQueued++;
                else if (wst == "ready") cntReady++;
                else if (wst == "awaiting" || wst == "awaiting_gate") cntGate++;
                else cntRunning++;
            }
        }

        // Sub: "N running · M queued · K done" triple + elapsed
        bool ja2 = _lang == 0;

        // TASK 5: run-ended state — when running==false AND every worker is terminal, show ended summary.
        bool allTerminal = false;
        int cntAttn = 0;
        int cntErr = 0;   // brief rule 5: error is its own danger count, not folded into needs-attention
        object wo3;
        if (root.TryGetValue("workers", out wo3) && wo3 is object[])
        {
            var wArr = (object[])wo3;
            if (wArr.Length > 0)
            {
                allTerminal = true;
                foreach (object ow2 in wArr)
                {
                    var ww2 = ow2 as Dictionary<string, object>;
                    if (ww2 == null) continue;
                    if (!IsTerminalWorker(ww2) && !IsInterruptedWorker(ww2)) { allTerminal = false; }
                    // ONE DEFINITION OF "needs attention" (Theme.CountsAsNeedsAttention), shared
                    // with the chat window's fleet band, plus the goal-based local_loop guard that
                    // needs the goal text. This is exactly IsOperatorAttention, spelled through the
                    // shared core so the two screens cannot drift apart again.
                    if (Theme.CountsAsNeedsAttention(S(ww2, "status"), S(ww2, "outcome"))
                        && !IsLocalLoopControlGoal(S(ww2, "goal"))) cntAttn++;
                    if (Theme.CountsAsError(S(ww2, "status"))) cntErr++;   // separate red tally
                }
            }
        }

        string triple;
        if (!running && allTerminal)
        {
            // Run-ended header: "{done} done · {attn} needs attention · run ended"
            if (ja2)
            {
                triple = cntDoneW + " 完了 · " + cntAttn + " 要対応 · " + (cntErr > 0 ? cntErr + " 停止(エラー) · " : "") + (cntIntr > 0 ? cntIntr + " 中断 · " : "") + "終了";
            }
            else
            {
                triple = cntDoneW + " done · " + cntAttn + " needs attention · " + (cntErr > 0 ? cntErr + " error · " : "") + (cntIntr > 0 ? cntIntr + " interrupted · " : "") + "run ended";
            }
        }
        else
        {
            triple = ja2
                ? (cntRunning + " 実行中 · " + cntReady + " 準備"
                   + (cntGate > 0 ? " · " + cntGate + " 承認待ち" : "")
                   + " · " + cntQueued + " 待機列 · " + cntDoneW + " 完了")
                : (cntRunning + " running · " + cntReady + " ready"
                   + (cntGate > 0 ? " · " + cntGate + " awaiting approval" : "")
                   + " · " + cntQueued + " queued · " + cntDoneW + " done");
        }
        // WHY the rest is not running, when the run says so itself (status.json max_concurrent).
        string waitReason = "";
        if (running && (cntReady > 0 || cntQueued > 0) && root.ContainsKey("max_concurrent"))
        {
            int mc = (int)Dbl(root, "max_concurrent");
            if (mc > 0 && cntRunning >= mc)
                waitReason = ja2 ? ("同時実行の上限 " + mc + " に達しているため、残りは順番待ちです")
                                 : ("Concurrency limit " + mc + " reached; the rest wait for a free slot");
            else if (mc > 0)
                waitReason = ja2 ? ("同時実行の上限 " + mc + "（空き枠を待っている、または枠を確保中）")
                                 : ("Concurrency limit " + mc + " (waiting for a free slot)");
        }

        // Live ETA: project remaining work from the throughput observed SO FAR (done/elapsed).
        string eta = "";
        if (running && total > 0 && done < total)
        {
            if (done > 0 && elapsed > 1.0)
            {
                double etaS = (total - done) * (elapsed / done);
                double perH = done / elapsed * 3600.0;
                eta = "    " + T("eta") + " " + DateTime.Now.AddSeconds(etaS).ToString("HH:mm")
                      + " (" + T("eta_in") + " " + Fmt(etaS) + " · "
                      + perH.ToString("0.#") + " " + T("rate") + ")";
            }
            else
            {
                eta = "    " + T("eta") + " " + T("eta_calc");
            }
        }

        // Two-stage stale indicator: soft (45 s) shows a calm wait hint; hard (90 s) shows a stop warning.
        // Derived from the runner PROCESS (pid + start time) and real file activity, not from
        // status.json's `updated` alone: a busy sweep legitimately goes >90 s silent.
        string staleKind, staleText;
        StaleVerdict(root, running, updated, out staleKind, out staleText);
        if (staleKind != "")
            triple = triple + " — " + staleText;

        // TASK 4: evidence freshness — "· evidence {N}s/m ago" from top-level `updated` field [COMPUTED].
        // Omit entirely if updated is missing (zero) — never show "unknown".
        string freshness = "";
        if (updated > 0)
        {
            double ageS = NowUnix() - updated;
            if (ageS >= 0)
            {
                string ageFmt;
                if (ageS < 60.0)
                    ageFmt = ((int)ageS).ToString() + (ja2 ? "秒" : "s");
                else
                    ageFmt = ((int)(ageS / 60.0)).ToString() + (ja2 ? "分" : "m");
                freshness = " · " + (ja2 ? "最終更新 " : "updated ") + ageFmt + (ja2 ? "前" : " ago");
            }
        }

        _sub.Text = triple + freshness + "    " + T("elapsed") + " " + Fmt(elapsed) + eta;
        _sub.ToolTip = _sub.Text;   // _sub itself is Collapsed (see BuildChrome) -- kept alive purely
                                    // as the plain-text fallback for any future consumer of its Text/
                                    // ToolTip; nothing else in this file reads them today (audited).

        // Feature 2: the single concatenated sentence above is what actually got read as a
        // fallback; what's actually DISPLAYED is a WrapPanel of discrete Pill() chips (same helper
        // HistoryRow etc. already use), one per fact, rebuilt from the SAME source variables (not
        // parsed back out of the sentence) so wording/pluralization can't drift between the two.
        if (_subChips != null)
        {
            _subChips.Children.Clear();
            if (!running && allTerminal)
            {
                _subChips.Children.Add(ChipMargin(Pill(cntDoneW + " " + (ja2 ? "完了" : "done"), "success")));
                _subChips.Children.Add(ChipMargin(Pill(cntAttn + " " + (ja2 ? "要対応" : "needs attention"),
                    cntAttn > 0 ? "warning" : "neutral")));
                if (cntErr > 0)
                    _subChips.Children.Add(ChipMargin(Pill(cntErr + " " + (ja2 ? "停止(エラー)" : "error"), "danger")));
                if (cntIntr > 0)
                    _subChips.Children.Add(ChipMargin(Pill(cntIntr + " " + (ja2 ? "中断" : "interrupted"), "warning")));
                _subChips.Children.Add(ChipMargin(Pill(ja2 ? "終了" : "run ended", "neutral")));
            }
            else
            {
                var runChip = Pill(cntRunning + " " + (ja2 ? "実行中" : "running"), "info");
                var readyChip = Pill(cntReady + " " + (ja2 ? "準備" : "ready"), "neutral");
                var queueChip = Pill(cntQueued + " " + (ja2 ? "待機列" : "queued"), "neutral");
                if (waitReason.Length > 0) { readyChip.ToolTip = waitReason; queueChip.ToolTip = waitReason; }
                _subChips.Children.Add(ChipMargin(runChip));
                _subChips.Children.Add(ChipMargin(readyChip));
                if (cntGate > 0)
                    _subChips.Children.Add(ChipMargin(Pill(cntGate + " " + (ja2 ? "承認待ち" : "awaiting approval"), "warning")));
                _subChips.Children.Add(ChipMargin(queueChip));
                _subChips.Children.Add(ChipMargin(Pill(cntDoneW + " " + (ja2 ? "完了" : "done"), "success")));
            }
            if (staleKind != "")
                _subChips.Children.Add(ChipMargin(Pill(staleText, staleKind)));
            // NOT CHIPS. A chip in this app means a state something is IN -- running, done,
            // needs attention -- and these three are clocks. Drawn in the same outlined pill as
            // the counts they were indistinguishable from them, so "0 needs attention" and
            // "elapsed 8m16s" arrived with equal weight and the row read as six alarms. They are
            // background facts; background facts are text.
            var meta = new List<string>();
            if (!string.IsNullOrEmpty(freshness)) meta.Add(freshness.TrimStart(' ', '·'));
            meta.Add(T("elapsed") + " " + Fmt(elapsed));
            if (!string.IsNullOrEmpty(eta)) meta.Add(eta.Trim());
            var metaTb = new TextBlock();
            metaTb.Text = string.Join("  ·  ", meta.ToArray());
            metaTb.Foreground = Theme.Br(Theme.Faint(_dark));
            metaTb.FontSize = 11;
            metaTb.VerticalAlignment = VerticalAlignment.Center;
            metaTb.Margin = new Thickness(4, 0, 0, 0);
            _subChips.Children.Add(metaTb);
            // THE THIRD GATE, ON THE ROW WHERE THE OTHER FACTS ALREADY LIVE. Disk and RAM have
            // had gauges for months. The limit that actually stopped a run -- 217 turns refused
            // out of 237 -- had none, so a fleet grinding to a halt looked like a fleet being
            // slow and the stop had no explanation anyone could point at.
            var rate = BuildRateStrip(root, ja2);
            if (rate != null) _subChips.Children.Add(rate);
            _subChips.ToolTip = _sub.Text;   // same fallback sentence, shown on hover over the chip row
        }
    }



    // ── Graduated severity colour ─────────────────────────────────────────────────────────
    //
    // WHY A GRADIENT AND NOT THREE BUCKETS. The first version of the rate strip used discrete
    // zones -- green under 60%, amber to 85%, red above -- which tells you WHICH BUCKET you are
    // in and not HOW CLOSE you are. 61% and 84% drew the same colour, so the whole span where
    // an operator could still act looked identical.
    //
    // The four anchors are the Atlassian Statuspage / status.claude.com uptime-bar ramp, taken
    // from that bar's own SVG rather than invented:
    //     green #76AD2A -> yellow #FAA72A -> orange #E86235 -> red #E04343
    // Interpolating a continuous severity across them means "degraded but nearly critical"
    // has its own colour, which is exactly the state worth seeing early.
    static readonly double[] _SEV_STOPS = { 0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0 };
    static readonly byte[][] _SEV_RGB = {
        new byte[] { 0x76, 0xAD, 0x2A },
        new byte[] { 0xFA, 0xA7, 0x2A },
        new byte[] { 0xE8, 0x62, 0x35 },
        new byte[] { 0xE0, 0x43, 0x43 },
    };

    // severity [0,1] -> colour, linearly interpolated between the anchors.
    static Color SeverityColor(double sev)
    {
        if (sev <= 0.0) return Color.FromRgb(_SEV_RGB[0][0], _SEV_RGB[0][1], _SEV_RGB[0][2]);
        if (sev >= 1.0) return Color.FromRgb(_SEV_RGB[3][0], _SEV_RGB[3][1], _SEV_RGB[3][2]);
        for (int i = 0; i < _SEV_STOPS.Length - 1; i++)
        {
            double s0 = _SEV_STOPS[i], s1 = _SEV_STOPS[i + 1];
            if (sev <= s1)
            {
                double t = (s1 > s0) ? (sev - s0) / (s1 - s0) : 0.0;
                byte[] c0 = _SEV_RGB[i], c1 = _SEV_RGB[i + 1];
                return Color.FromRgb(
                    (byte)Math.Round(c0[0] + (c1[0] - c0[0]) * t),
                    (byte)Math.Round(c0[1] + (c1[1] - c0[1]) * t),
                    (byte)Math.Round(c0[2] + (c1[2] - c0[2]) * t));
            }
        }
        return Color.FromRgb(_SEV_RGB[3][0], _SEV_RGB[3][1], _SEV_RGB[3][2]);
    }

    // A raw metric -> severity, piecewise linear through the same three thresholds the sibling
    // status page uses: at or below `green` it is 0; `deg` lands on the yellow anchor; `crit`
    // and beyond is 1. The deg..crit leg ramps continuously so approaching the ceiling darkens
    // steadily instead of flipping at a boundary.
    static double PiecewiseSeverity(double x, double green, double deg, double crit)
    {
        const double Y = 1.0 / 3.0;
        if (x <= green) return 0.0;
        if (x <= deg) return (deg > green) ? Y * (x - green) / (deg - green) : Y;
        if (x <= crit) return (crit > deg) ? Y + (1.0 - Y) * (x - deg) / (crit - deg) : 1.0;
        return 1.0;
    }

    // The rate gauge's own mapping. Half the published ceiling is unambiguously fine; from
    // there it ramps, and 100% of the published figure is full red -- not because the run
    // stops there, but because that is the last point at which the number still means anything.
    static double RateSeverity(double pct) { return PiecewiseSeverity(pct, 50.0, 75.0, 100.0); }

    // The turn meter, read straight off disk so the gauge does not depend on a fleet running.
    // JSONL, one record per turn or refusal; bucketed here into the same shape fleet_runner
    // publishes so the strip has one code path whichever source it came from. Never throws --
    // a gauge that can take down the header is worse than no gauge.
    Dictionary<string, object> ReadQuotaMeter()
    {
        try
        {
            // Derived from the status path we already hold. The first version named a
            // _fleetDir field that does not exist -- the third invented identifier tonight,
            // and the third caught by the compiler rather than by review.
            string path = Path.Combine(Path.GetDirectoryName(_statusPath), "quota_meter.jsonl");
            if (!File.Exists(path)) return null;
            double now = NowUnix();
            var minutes = new int[60];
            int rpm = 0, rph = 0, refusals = 0;
            bool any = false;
            foreach (string line in File.ReadAllLines(path, Encoding.UTF8))
            {
                string t = line.Trim();
                if (t.Length == 0) continue;
                Dictionary<string, object> row = null;
                try { row = _js.DeserializeObject(t) as Dictionary<string, object>; } catch { }
                if (row == null) continue;
                double ts = 0;
                object tsRaw;
                if (row.TryGetValue("ts", out tsRaw) && tsRaw != null)
                { try { ts = Convert.ToDouble(tsRaw); } catch { } }
                double age = now - ts;
                if (age < 0 || age >= 3600) continue;
                string ev = S(row, "event");
                if (ev == "turn")
                {
                    any = true; rph++;
                    if (age < 60) rpm++;
                    int idx = 59 - (int)(age / 60.0);
                    if (idx >= 0 && idx < 60) minutes[idx]++;
                }
                else if (ev == "refusal" && age < 300)
                {
                    any = true; refusals++;
                }
            }
            if (!any) return null;
            // The USER's ceiling, not the published one. A strip drawn against 100 while
            // admission holds at 40 would show comfortable headroom at the exact moment the
            // fleet stopped admitting -- the gauge would be explaining the opposite of what
            // was happening.
            double limitRpm = _rateCeiling > 0 ? (double)_rateCeiling : 100.0;
            var series = new object[60];
            for (int i = 0; i < 60; i++) series[i] = minutes[i];
            var q = new Dictionary<string, object>();
            q["rpm"] = rpm; q["rph"] = rph;
            q["limit_rpm"] = limitRpm;
            q["pct_rpm"] = 100.0 * rpm / limitRpm;
            q["refusals_5m"] = refusals;
            q["series_rpm"] = series;
            q["measured"] = true;
            return q;
        }
        catch { return null; }
    }

    // ── The Copilot rate-limit strip ──────────────────────────────────────────────────────
    //
    // WHY IT EXISTS. A dense run had 217 of its 237 turns refused and nothing on this screen
    // said so. Disk and RAM gates have been drawn for months; the quota that actually stopped
    // the run was invisible, so "the fleet is slow" and "the fleet is being refused" looked
    // identical to whoever was watching. This sits on the header row beside the counts and the
    // clocks, because that is where someone already looks to ask how the run is going.
    //
    // WHAT IT DRAWS, AND THE UNIT MATTERS. Turns per minute -- generative messages, which is
    // what Microsoft publishes a limit for. NOT the MCP tool calls our own gateway sees: a turn
    // may make none of those or several, and measuring the number we happened to have is how
    // the first headroom estimate came out wrong by an unknown factor.
    //
    // THE LINE IS A REFERENCE, NOT A PROMISE. 100/minute is the documented Microsoft 365
    // Copilot row, scoped per Dataverse environment, and Microsoft says downstream services may
    // impose lower ones. Refusals are therefore shown BESIDE the bar and never derived from it:
    // refusals arriving while the bar looks comfortable mean the line is the wrong line, which
    // is a finding rather than a contradiction the panel should hide.
    UIElement BuildRateStrip(Dictionary<string, object> root, bool ja)
    {
        // READ THE METER DIRECTLY, and fall back to whatever the run put in status.json.
        //
        // The first version only read status.json's "quota", which meant the gauge appeared
        // solely while a fleet was running and writing that file -- and vanished the moment it
        // was most wanted. The rate limit is a property of the TENANT, not of one run: "am I
        // still being refused, or has it recovered" is a question asked with nothing running.
        // It also meant a status.json written before this key existed showed no gauge at all,
        // which is exactly what happened on the first build.
        var q = ReadQuotaMeter();
        if (q == null && root != null)
        {
            object qRaw;
            if (root.TryGetValue("quota", out qRaw)) q = qRaw as Dictionary<string, object>;
        }
        if (q == null) return null;

        int rpm = I(q, "rpm");
        int rph = I(q, "rph");
        int refusals = I(q, "refusals_5m");
        double limitRpm = _rateCeiling > 0 ? (double)_rateCeiling : 100.0;
        double pct = 0.0;
        try { limitRpm = Convert.ToDouble(q["limit_rpm"]); } catch { }
        // The setting wins over whatever the writer stamped: fleet_runner records limit_rpm
        // from the module default, and the operator may have set a lower line since.
        if (_rateCeiling > 0) limitRpm = _rateCeiling;
        pct = 100.0 * rpm / Math.Max(1.0, limitRpm);
        // pct is derived from the ceiling above, NOT read back from the file: the writer
        // computed its percentage against the default limit and using it here would draw the
        // bar against one line while the number beside it counts against another.
        bool measured = false;
        object mRaw;
        if (q.TryGetValue("measured", out mRaw) && mRaw != null)
        { try { measured = Convert.ToBoolean(mRaw); } catch { } }

        var row = new StackPanel();
        row.Orientation = Orientation.Horizontal;
        row.VerticalAlignment = VerticalAlignment.Center;
        row.Margin = new Thickness(20, 0, 0, 0);

        var lbl = new TextBlock();
        lbl.Text = ja ? "レート" : "rate";
        lbl.Foreground = Theme.Br(Theme.Faint(_dark));
        lbl.FontSize = 11;
        lbl.VerticalAlignment = VerticalAlignment.Center;
        lbl.Margin = new Thickness(0, 0, 6, 0);
        row.Children.Add(lbl);

        // The reading. "--" when nothing has been measured: an empty meter and a quiet one look
        // identical as a number, and only one of them means there is headroom.
        var val = new TextBlock();
        val.FontSize = 11.5;
        val.FontWeight = FontWeights.SemiBold;
        val.Foreground = Theme.Br(Theme.Text(_dark));
        val.VerticalAlignment = VerticalAlignment.Center;
        val.Text = measured ? (rpm + "/" + ((int)limitRpm)) : "--";
        row.Children.Add(val);

        var per = new TextBlock();
        per.Text = ja ? " /分" : " /min";
        per.Foreground = Theme.Br(Theme.Faint(_dark));
        per.FontSize = 10;
        per.VerticalAlignment = VerticalAlignment.Center;
        per.Margin = new Thickness(0, 0, 8, 0);
        row.Children.Add(per);

        // A short bar so the proportion reads without arithmetic.
        var track = new Border();
        track.Width = 54; track.Height = 5;
        track.CornerRadius = new CornerRadius(2.5);
        track.Background = Theme.Br(Theme.Border(_dark));
        track.VerticalAlignment = VerticalAlignment.Center;
        track.Margin = new Thickness(0, 0, 8, 0);
        var holder = new Grid();
        holder.HorizontalAlignment = HorizontalAlignment.Left;
        var fill = new Border();
        fill.Height = 5;
        fill.CornerRadius = new CornerRadius(2.5);
        fill.HorizontalAlignment = HorizontalAlignment.Left;
        fill.Background = new SolidColorBrush(SeverityColor(RateSeverity(pct)));
        fill.Width = measured ? Math.Max(1.5, Math.Min(54.0, 54.0 * pct / 100.0)) : 0.0;
        holder.Children.Add(fill);
        track.Child = holder;
        row.Children.Add(track);

        // THE LAST HOUR, so a BURST is visible. A single number cannot show one, and a burst is
        // exactly what trips a per-minute limit while the hourly total still looks fine.
        object sRaw;
        if (q.TryGetValue("series_rpm", out sRaw) && sRaw is object[])
        {
            object[] series = (object[])sRaw;
            int peak = 1;
            for (int i = 0; i < series.Length; i++)
            {
                int v = 0;
                try { v = Convert.ToInt32(series[i]); } catch { }
                if (v > peak) peak = v;
            }
            double scale = Math.Max((double)peak, limitRpm);
            var spark = new StackPanel();
            spark.Orientation = Orientation.Horizontal;
            spark.Height = 14;
            spark.VerticalAlignment = VerticalAlignment.Center;
            spark.Margin = new Thickness(0, 0, 8, 0);
            for (int i = 0; i < series.Length; i++)
            {
                int v = 0;
                try { v = Convert.ToInt32(series[i]); } catch { }
                var bar = new Border();
                bar.Width = 1.6;
                bar.Margin = new Thickness(0, 0, 0.6, 0);
                bar.VerticalAlignment = VerticalAlignment.Bottom;
                bar.Height = (v <= 0) ? 1.0 : Math.Max(1.5, 14.0 * v / scale);
                // EACH MINUTE KEEPS ITS OWN COLOUR. A burst twenty minutes ago stays red in
                // the history even though the current reading is green -- which is the whole
                // reason to draw an hour instead of a number.
                bar.Background = (v <= 0)
                    ? Theme.Br(Theme.Border(_dark))
                    : new SolidColorBrush(SeverityColor(RateSeverity(100.0 * v / Math.Max(1.0, limitRpm))));
                spark.Children.Add(bar);
            }
            row.Children.Add(spark);
        }

        // REFUSALS, BESIDE THE BAR AND NEVER DERIVED FROM IT. This is the line that explains a
        // stop: if it is non-zero the fleet is being refused, whatever the bar says.
        if (refusals > 0)
        {
            var warn = new TextBlock();
            warn.Text = ja ? ("拒否 " + refusals + " /5分") : (refusals + " refused /5m");
            warn.Foreground = new SolidColorBrush(SeverityColor(1.0));
            warn.FontSize = 11;
            warn.FontWeight = FontWeights.SemiBold;
            warn.VerticalAlignment = VerticalAlignment.Center;
            row.Children.Add(warn);
        }

        string tip = measured
            ? (ja
               ? ("生成メッセージ " + rpm + " /分（公開上限 " + ((int)limitRpm) + "）、直近1時間 " + rph + " 件。\n"
                  + "上限は Dataverse 環境単位の公開値で、下位サービスがより低い制限を課すことがあります。\n"
                  + "余裕があるのに拒否が出ている場合、効いている制限は公開値ではありません。")
               : ("generative messages " + rpm + "/min (published limit " + ((int)limitRpm) + "), "
                  + rph + " in the last hour.\nThe limit is the published per-Dataverse-environment "
                  + "figure; downstream services may impose lower ones.\nRefusals with headroom to "
                  + "spare mean the binding limit is not the published one."))
            : (ja ? "まだ1ターンも計測していません。" : "no turns measured yet.");
        row.ToolTip = tip;
        return row;
    }

    // Roll seconds up through y/d/h/m/s so a long run reads "1h39m27s" / "1d1h1m1s" instead of
    // an unbounded "99m27s". Show every unit from the highest non-zero down to seconds (so
    // ordering is unambiguous). Month is intentionally omitted: its token would clash with
    // minutes ('m') and its length is variable; a 365-day year is a good-enough cap for run time.
    static string Fmt(double sec)
    {
        if (sec < 0) sec = 0;
        long s = (long)sec;
        long y = s / 31536000; s -= y * 31536000;
        long d = s / 86400;    s -= d * 86400;
        long h = s / 3600;     s -= h * 3600;
        long m = s / 60;       s -= m * 60;
        var sb = new StringBuilder();
        if (y > 0)                  sb.Append(y).Append('y');
        if (sb.Length > 0 || d > 0) sb.Append(d).Append('d');
        if (sb.Length > 0 || h > 0) sb.Append(h).Append('h');
        if (sb.Length > 0 || m > 0) sb.Append(m).Append('m');
        sb.Append(s).Append('s');
        return sb.ToString();
    }
}
