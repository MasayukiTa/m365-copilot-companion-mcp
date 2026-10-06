// StartupGateHarness.cs -- TEST-ONLY. Drives StartupGate.GateAutomaticLaunch and
// AutoFixBudget.Decide (both in the SHIPPED ui/SelfImproveDashboard.cs) against synthetic
// inputs, for ui/test_the_startup_loop_has_a_backstop.py. Not in any Build line: see
// COMPILED_BY_A_TEST in ui/test_one_list_says_what_the_ui_compiles.py.
//
// WHY A HARNESS AT ALL: both classes are pure (no file/process I/O), extracted from
// FleetCockpit.cs specifically so the 2026-09-24 startup-loop fix could be driven directly with
// synthetic inputs instead of staging a real start_all.ps1 mutex, a real M365_LAUNCHED_BY_START_ALL
// environment, and a real .fleet/autofix_budget.json on disk. Driving THIS function is driving
// the shipped decision, not a re-implementation of it.
//
//   StartupGateHarness.exe gate <cases.json> <results.json>
//       cases.json:   [{"startAllRunning":bool,"launchedByStartAll":bool,"isFirstSweep":bool}, ...]
//       results.json: [{"allow":bool,"reasonKey":str}, ...]
//
//   StartupGateHarness.exe budget_sequence <cases.json> <results.json>
//       Each case is a KEY's whole history threaded through consecutive Decide calls, the
//       output "Kept" of one call feeding the "attempts" input of the next -- this is exactly
//       the read-decide-write cycle TryConsumeAutoFixBudget performs against the real json
//       file in FleetCockpit.cs, so it is how "the budget persists across two processes" (two
//       reads of what the first call wrote) is exercised without touching a real file.
//       cases.json:   [{"maxAttempts":int,"windowS":number,"steps":[{"now":number}, ...]}, ...]
//       results.json: [{"steps":[{"allow":bool,"exhausted":bool,"keptCount":int}, ...]}, ...]
//
// Legacy csc, C# 5.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

static class StartupGateHarness
{
    static readonly JavaScriptSerializer _js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

    static int Main(string[] args)
    {
        try
        {
            if (args.Length == 3 && args[0] == "gate") return Gate(args[1], args[2]);
            if (args.Length == 3 && args[0] == "budget_sequence") return BudgetSequence(args[1], args[2]);
            Console.Error.WriteLine("usage: gate <cases.json> <results.json> | budget_sequence <cases.json> <results.json>");
            return 2;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("harness crashed: " + ex);
            return 3;
        }
    }

    static int Gate(string casesPath, string resultsPath)
    {
        var cases = _js.DeserializeObject(File.ReadAllText(casesPath, Encoding.UTF8)) as object[];
        if (cases == null) { Console.Error.WriteLine("cases.json is not a JSON array"); return 4; }
        var results = new List<object>();
        foreach (object co in cases)
        {
            var c = (Dictionary<string, object>)co;
            bool startAllRunning = Convert.ToBoolean(c["startAllRunning"]);
            bool launchedByStartAll = Convert.ToBoolean(c["launchedByStartAll"]);
            bool isFirstSweep = Convert.ToBoolean(c["isFirstSweep"]);

            StartupGate.Decision d = StartupGate.GateAutomaticLaunch(startAllRunning, launchedByStartAll, isFirstSweep);

            var row = new Dictionary<string, object>();
            row["allow"] = d.Allow;
            row["reasonKey"] = d.ReasonKey ?? "";
            results.Add(row);
        }
        File.WriteAllText(resultsPath, _js.Serialize(results), new UTF8Encoding(false));
        return 0;
    }

    static int BudgetSequence(string casesPath, string resultsPath)
    {
        var cases = _js.DeserializeObject(File.ReadAllText(casesPath, Encoding.UTF8)) as object[];
        if (cases == null) { Console.Error.WriteLine("cases.json is not a JSON array"); return 4; }
        var caseResults = new List<object>();
        foreach (object co in cases)
        {
            var c = (Dictionary<string, object>)co;
            int maxAttempts = Convert.ToInt32(c["maxAttempts"]);
            double windowS = Convert.ToDouble(c["windowS"]);
            var steps = c["steps"] as object[];

            var attempts = new List<double>();
            var stepResults = new List<object>();
            if (steps != null)
            {
                foreach (object so in steps)
                {
                    var s = (Dictionary<string, object>)so;
                    double now = Convert.ToDouble(s["now"]);

                    AutoFixBudget.Decision d = AutoFixBudget.Decide(attempts, now, maxAttempts, windowS);
                    attempts = d.Kept;   // simulates the next "process" reading what this one wrote

                    var row = new Dictionary<string, object>();
                    row["allow"] = d.Allow;
                    row["exhausted"] = d.Exhausted;
                    row["keptCount"] = d.Kept.Count;
                    stepResults.Add(row);
                }
            }
            var caseRow = new Dictionary<string, object>();
            caseRow["steps"] = stepResults;
            caseResults.Add(caseRow);
        }
        File.WriteAllText(resultsPath, _js.Serialize(caseResults), new UTF8Encoding(false));
        return 0;
    }
}
