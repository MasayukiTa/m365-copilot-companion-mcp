using System;
using System.Collections.Generic;

// ── Startup / auto-repair gating, extracted here (not FleetCockpit.cs) for the same reason
// FrozenGate lived here 2026-09-19 to 2026-09-24: this file has no Main, so a test harness can
// compile it standalone (+ Theme.cs, +WPF refs) without colliding with CockpitProgram's Main in
// FleetCockpit.cs. Both classes are used by FleetCockpit.cs's health-poll / auto-repair code;
// neither does file or process I/O itself.
//
// THE INCIDENT THIS EXISTS TO FIX (2026-09-24, ~07:35-07:53): the startup banner kept
// reappearing and startups queued behind each other for ~20 minutes. Mechanism, read off
// process trees and .fleet/autofix.jsonl:
//   (a) FleetCockpit's health-poll auto-heal called RunStartAll() on its OWN first sweep
//       whenever server/tunnel read Red -- with no check for whether a start_all.ps1 was
//       ALREADY mid-bring-up, including the one that had just launched THIS cockpit process.
//       start_all.ps1 relaunches a stale UI while it is still starting the backend, so the new
//       cockpit saw the same Red and launched ANOTHER start_all, which queued on start_all's
//       own single-instance lock ("another start_all is already running... waiting for it to
//       finish"), then relaunched the cockpit again.
//   (b) The retry budget (AUTOFIX_MAX_ATTEMPTS) and RunStartAll's 120s cooldown lived in that
//       process's memory alone, so every relaunched cockpit got a FRESH budget -- the loop in
//       (a) never ran into either backstop.
//   (c) repair.ps1 -Auto's Tier A repair for server/tunnel is itself another start_all.ps1
//       invocation (`-NoUi -NoSplash`), so a repair triggered while start_all was already
//       running added a second queued startup, not a fix; and unparsable repair.ps1 output
//       fell back to a blind RunStartAll(), a THIRD way to add one.
internal static class StartupGate
{
    internal struct Decision
    {
        public bool Allow;
        public string ReasonKey;   // "" when Allow, or when disallowed for a reason nobody
                                   // needs telling about; else a T() key to show the operator.
    }

    // startAllRunning: the SAME cross-process signal start_all.ps1's Enter-StartAllLock uses --
    //   the "Global\m365-copilot-companion-start-all" mutex, currently held by another process.
    //   Checked before EVERY automatic RunStartAll or repair(.ps1) launch, not just the first
    //   sweep: repair.ps1's Tier A for server/tunnel is itself a start_all.ps1 invocation, so
    //   the same "don't add a second queued startup" rule applies to it too.
    // launchedByStartAll: this process's own M365_LAUNCHED_BY_START_ALL=1 environment flag,
    //   set only in the child process start_all.ps1 itself launches (scripts/start_all.ps1,
    //   the Start-Process block under "THE WINDOW MAY ASK 'WHO OPENED ME?'"). No start_all.ps1
    //   change is needed: the flag already exists there and was simply never read here.
    // isFirstSweep: true ONLY for the once-per-process startup auto-heal check. The ongoing
    //   MaybeAutoFix loop and the manual Fix button are NOT first-sweep and are gated by
    //   AutoFixBudget below instead, not by launchedByStartAll -- a cockpit start_all launched
    //   can still need automatic repair ten minutes later, once start_all itself is long done,
    //   and that later need must not be silenced by how this process was born.
    internal static Decision GateAutomaticLaunch(bool startAllRunning, bool launchedByStartAll, bool isFirstSweep)
    {
        if (startAllRunning)
            return new Decision { Allow = false, ReasonKey = "autofix_start_all_running" };
        if (isFirstSweep && launchedByStartAll)
            // Silent: nothing is wrong here. start_all just finished launching this very
            // process; assuming its own bring-up already failed within the first ~15s of this
            // cockpit's life is the assumption that caused the loop.
            return new Decision { Allow = false, ReasonKey = "" };
        return new Decision { Allow = true, ReasonKey = "" };
    }
}

// A cross-process, bounded-backoff retry budget: at most `maxAttempts` within a trailing
// `windowS`-second window, PER KEY, persisted to disk by the caller so a new process does not
// get a fresh budget (bug (b) above). No I/O here on purpose -- FleetCockpit.cs loads a key's
// recent attempt timestamps from .fleet/autofix_budget.json, calls Decide, and writes back
// `Kept` regardless of the outcome (so a stale, long-exhausted key's timestamps still age out
// of the window on the next read instead of pinning it exhausted forever).
internal static class AutoFixBudget
{
    internal struct Decision
    {
        public bool Allow;
        public bool Exhausted;              // Allow == false AND the cap (not some other reason) is why
        public List<double> Kept;           // what the caller should persist for this key
    }

    internal static Decision Decide(List<double> attempts, double now, int maxAttempts, double windowS)
    {
        var kept = new List<double>();
        if (attempts != null)
            foreach (double t in attempts)
                if (now - t < windowS) kept.Add(t);
        bool allow = kept.Count < maxAttempts;
        // Recording IS part of the decision, not a separate step: a caller that got Allow=true
        // and then decided not to actually launch (a race with another gate) still consumed one
        // slot of the budget, which is the conservative (under- not over-budget) side to err on.
        if (allow) kept.Add(now);
        return new Decision { Allow = allow, Exhausted = !allow, Kept = kept };
    }
}
