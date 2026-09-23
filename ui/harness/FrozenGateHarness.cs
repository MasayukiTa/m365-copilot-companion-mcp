// FrozenGateHarness.cs -- TEST-ONLY. Drives SelfImproveDashboardWindow.FrozenGate.Decide (in
// the SHIPPED ui/SelfImproveDashboard.cs) against synthetic inputs, for
// ui/test_the_frozen_dot_is_hidden_when_self_improvement_is_unused.py. Not in any Build line:
// see COMPILED_BY_A_TEST in ui/test_one_list_says_what_the_ui_compiles.py.
//
// WHY A HARNESS AT ALL, RATHER THAN STAGING REAL BASELINE/ANCHOR FILES: FrozenGate.Decide is a
// pure function of three already-computed facts (inUse, ok, drift) -- it does no file I/O of
// its own. Driving it directly is a smaller, faster, more exhaustive test than reconstructing
// every combination of a real frozen_baseline.json and a real ~/.selfimprove_frozen_anchor on
// disk, and it is the SAME function FleetCockpit.cs's health poll calls, not a re-implementation
// of its logic -- so a passing test here is a fact about the shipped decision, not about a copy
// of it.
//
//   FrozenGateHarness.exe run <cases.json> <results.json>
//       cases.json:   [{"inUse":bool,"ok":bool,"drift":[string,...]}, ...]
//       results.json: [{"visible":bool,"color":"gray"|"green"|"yellow"|"red","detailKey":str}, ...]
//
// Legacy csc, C# 5 (matches every other file this project compiles this way).
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

static class FrozenGateHarness
{
    static readonly JavaScriptSerializer _js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

    static int Main(string[] args)
    {
        try
        {
            if (args.Length == 3 && args[0] == "run") return Run(args[1], args[2]);
            Console.Error.WriteLine("usage: run <cases.json> <results.json>");
            return 2;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("harness crashed: " + ex);
            return 3;
        }
    }

    static int Run(string casesPath, string resultsPath)
    {
        var cases = _js.DeserializeObject(File.ReadAllText(casesPath, Encoding.UTF8)) as object[];
        if (cases == null) { Console.Error.WriteLine("cases.json is not a JSON array"); return 4; }
        var results = new List<object>();
        foreach (object co in cases)
        {
            var c = (Dictionary<string, object>)co;
            bool inUse = Convert.ToBoolean(c["inUse"]);
            bool ok = Convert.ToBoolean(c["ok"]);
            var driftArr = c.ContainsKey("drift") ? c["drift"] as object[] : null;
            var drift = new List<string>();
            if (driftArr != null)
                foreach (object d in driftArr) drift.Add(Convert.ToString(d));

            var r = SelfImproveDashboardWindow.FrozenGate.Decide(inUse, ok, drift);

            var row = new Dictionary<string, object>();
            row["visible"] = r.Visible;
            row["color"] = r.Color;
            row["detailKey"] = r.DetailKey;
            results.Add(row);
        }
        File.WriteAllText(resultsPath, _js.Serialize(results), new UTF8Encoding(false));
        return 0;
    }
}
