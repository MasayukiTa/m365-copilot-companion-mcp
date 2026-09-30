// EffortPolicyHarness.cs -- TEST-ONLY. Drives ui/EffortPolicy.cs (the shipped file) for
// ui/test_the_effort_policy_says_what_is_in_effect.py. Not in any Build line: see
// COMPILED_BY_A_TEST in ui/test_one_list_says_what_the_ui_compiles.py.
//
//   EffortPolicyHarness.exe <cases.json> <results.json>
//
// cases: [{"op":"parse","line":..}, {"op":"describe","mode":..,"source":..,"conflict":..,"ja":..},
//         {"op":"conflict","env":..,"selected":..,"conflict":..,"ja":..},
//         {"op":"badge","level":..,"source":..,"last":{..}|null,"ja":..}]
// results: one {"text":..,"tip":..} (or {"text":..}) per case, null for "nothing shown".
//
// Legacy csc, C# 5.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

static class EffortPolicyHarness
{
    static string Str(Dictionary<string, object> d, string k)
    {
        object v;
        return (d.TryGetValue(k, out v) && v != null) ? v.ToString() : null;
    }

    static bool Bool(Dictionary<string, object> d, string k)
    {
        object v;
        return d.TryGetValue(k, out v) && v is bool && (bool)v;
    }

    static int Main(string[] args)
    {
        try
        {
            if (args.Length != 2) { Console.Error.WriteLine("usage: <cases.json> <results.json>"); return 2; }
            var js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
            var cases = js.DeserializeObject(File.ReadAllText(args[0], Encoding.UTF8)) as object[];
            if (cases == null) return 4;
            var results = new List<object>();
            foreach (object o in cases)
            {
                var c = (Dictionary<string, object>)o;
                string op = Str(c, "op");
                var r = new Dictionary<string, object>();
                if (op == "parse") r["text"] = EffortPolicyView.ParseMode(Str(c, "line"));
                else if (op == "describe")
                    r["text"] = EffortPolicyView.Describe(Str(c, "mode"), Str(c, "source"), Bool(c, "conflict"), Bool(c, "ja"));
                else if (op == "conflict")
                    r["text"] = EffortPolicyView.ConflictText(Str(c, "env"), Str(c, "selected"), Bool(c, "conflict"), Bool(c, "ja"));
                else if (op == "badge")
                {
                    object last;
                    c.TryGetValue("last", out last);
                    var ld = last as Dictionary<string, object>;
                    r["text"] = EffortPolicyView.BadgeText(Str(c, "level"), Str(c, "source"), ld, Bool(c, "ja"));
                    r["tip"] = EffortPolicyView.BadgeTip(Str(c, "level"), Str(c, "source"), ld, Bool(c, "ja"));
                }
                else return 5;
                results.Add(r);
            }
            File.WriteAllText(args[1], js.Serialize(results), new UTF8Encoding(false));
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("harness crashed: " + ex);
            return 3;
        }
    }
}
