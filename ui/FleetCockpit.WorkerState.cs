// FleetCockpit.WorkerState.cs -- worker state predicates: attention, retryable, terminal, interrupted, severity rank.
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

    // Pass A2-1: true when a status is a terminal-but-unresolved attention lane (stuck/maxturns/error).
    // These get the "recovery surface" treatment on their collapsed row (§6 spec).
    static bool IsAttentionStatus(string status)
    {
        return status == "stuck" || status == "maxturns" || status == "error";
    }

    static bool IsAgentSetupRuntimeWait(string reason)
    {
        if (string.IsNullOrWhiteSpace(reason)) return false;
        return reason.IndexOf("Agent Instructions are missing or stale", StringComparison.OrdinalIgnoreCase) >= 0
            && reason.IndexOf("browser answered RUN without a SQLite commit", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    static bool IsLocalLoopControlGoal(string goal)
    {
        string norm = (goal ?? "").Replace("\r", " ").Replace("\n", " ").Replace("\t", " ").Trim().ToLowerInvariant();
        while (norm.Contains("  ")) norm = norm.Replace("  ", " ");
        return norm.StartsWith("local_loop run ")
            || norm.StartsWith("local_loop bootstrap ")
            || norm.StartsWith("local_loop protocol ")
            || norm.StartsWith("execute local_loop job ")
            || norm.StartsWith("run local_loop job ");
    }

    static bool IsOperatorAttention(Dictionary<string, object> w)
    {
        return w != null
            && !IsLocalLoopControlGoal(S(w, "goal"))
            && !IsInfraStuck(w)
            && IsAttentionStatus(S(w, "status"));
    }

    static bool IsRetryableWorker(Dictionary<string, object> w)
    {
        if (w == null) return false;
        // The worker's own verdict beats the outcome-wide rule: `retryable: false` is written when
        // re-running could duplicate work (e.g. an ambiguous fresh submit, outcome STUCK).
        object rv;
        if (w.TryGetValue("retryable", out rv) && rv is bool && !(bool)rv) return false;
        return !IsLocalLoopControlGoal(S(w, "goal"))
            && IsRetryableOutcome(S(w, "outcome"));
    }

    // One key per worker instance (jid when the runner minted one, else name + run_id), so a
    // terminal STUCK worker is re-queued at most once per cockpit session.
    static string RetryWorkerKey(Dictionary<string, object> w)
    {
        string jid = S(w, "jid");
        if (!string.IsNullOrEmpty(jid)) return "jid:" + jid;
        return "w:" + S(w, "name") + "|" + S(w, "run_id");
    }

    // True when this terminal worker has already been re-queued (by us, or by the runner), or an
    // identical goal is queued/running now -- i.e. another copy exists and must not be added.
    bool RetryAlreadyCovered(Dictionary<string, object> root, Dictionary<string, object> w)
    {
        if (_autoRetriedWorkers.Contains(RetryWorkerKey(w))) return true;
        object rq;
        if (w.TryGetValue("retry_queued", out rq) && rq is bool && (bool)rq) return true;
        string goal = S(w, "goal");
        object wo;
        if (root != null && root.TryGetValue("workers", out wo) && wo is object[])
        {
            foreach (object o in (object[])wo)
            {
                var x = o as Dictionary<string, object>;
                if (x == null || object.ReferenceEquals(x, w)) continue;
                if (IsTerminalWorker(x)) continue;
                if (S(x, "goal") == goal) return true;
            }
        }
        return false;
    }

    // P0: an INFRA_STUCK worker is NOT a task failure — the engine parked it because the infra
    // (Edge session / sign-in / connector) broke. It gets a distinct ORANGE インフラ待ち pill,
    // is excluded from failure/miss counts, and its `reason` (actionable) is shown as-is.
    static bool IsInfraStuck(Dictionary<string, object> w)
    {
        return string.Equals(S(w, "outcome"), "INFRA_STUCK", StringComparison.OrdinalIgnoreCase);
    }

    // MIRRORS relay/outcomes.py RETRYABLE. A copy, and copies drift -- so a test fails if
    // this stops matching the Python set. An outcome missing from here is not retried, which
    // is the safe direction; an outcome wrongly added here re-runs finished work.
    static readonly string[] _retryableOutcomes = { "STUCK", "INFRA_STUCK", "REFUSED" };

    static bool IsRetryableOutcome(string outcome)
    {
        if (string.IsNullOrEmpty(outcome)) return false;
        foreach (string r in _retryableOutcomes)
            if (outcome == r) return true;
        return false;
    }

    static bool IsTerminalWorker(Dictionary<string, object> w)
    {
        // MUST MATCH relay/relay_fleet.py TERMINAL. A status missing here does more than skip
        // one card: MaybeAutoArchive returns early the moment any worker looks unfinished, so
        // one unrecognised status costs the whole run its auto-archive.
        string status = S(w, "status");
        return status == "done" || status == "stuck" || status == "maxturns"
               || status == "error" || status == "cancelled"
               || status == "content_refused";
    }

    // relay/fleet_reaper.py writes status "interrupted" into the sidecars of a coordinator that
    // DIED (crash, kill, disk full). It is NOT terminal (never add it to IsTerminalWorker: the
    // work is resumable and must not be archived or counted as finished) and it is NOT running
    // and NOT a failure of the goal. Mirrors relay/outcomes.py STATUS_OF["INTERRUPTED"].
    static bool IsInterruptedWorker(Dictionary<string, object> w)
    {
        return w != null && S(w, "status") == "interrupted";
    }

    // Severity rank for the "unfinished only" sort: failures first, then max-turns, then
    // cancelled, then still-running/other (stable within a rank).
    static int SeverityRank(Dictionary<string, object> w)
    {
        string oc = S(w, "outcome");
        if (oc == "STUCK" || oc == "ERROR") return 0;
        if (oc == "MAXTURNS") return 1;
        if (oc == "CANCELLED") return 2;
        if (oc == "INTERRUPTED") return 2;
        return 3;   // still-running / other
    }
}
