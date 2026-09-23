// SubmittedTasksHarness.cs -- TEST-ONLY. Drives ui/SubmittedTasks.cs (the shipped file) against
// real files, for ui/test_a_submitted_task_is_on_top_at_once.py. Not in any Build line: see
// COMPILED_BY_A_TEST in ui/test_one_list_says_what_the_ui_compiles.py.
//
//   SubmittedTasksHarness.exe write <stateDir> <texts.json>
//       Writes one add_goal command per text through the SHIPPED writer, FleetCommands.Write,
//       in the shape both exes send ({"add_goal":[{"text":..,"priority":false}]}).
//   SubmittedTasksHarness.exe run <cases.json> <results.json>
//       Runs each case's steps against ONE SubmittedTasks instance and records what it shows.
//
// Steps:
//   {"op":"local","goal":..,"now":..,"status":<status.json path or null>}
//   {"op":"view","now":..,"state":<.fleet dir>,"tasks":<tasks dir>,"status":<path or null>}
//   {"op":"dismiss","key":..,"now":..}
//
// Legacy csc, C# 5.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

static class SubmittedTasksHarness
{
    static readonly JavaScriptSerializer _js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

    //: The cockpit's terminal set for ORDERING: FleetCockpit.IsTerminalWorker plus "freed"
    //: (FleetCockpit.IsTerminalForOrder). Restated here because the harness cannot link the WPF
    //: file; the order under test is Compose's, which takes the predicate as an input.
    static bool IsTerminal(Dictionary<string, object> w)
    {
        object v;
        string s = (w != null && w.TryGetValue("status", out v) && v != null) ? v.ToString() : "";
        return s == "done" || s == "stuck" || s == "maxturns" || s == "error" || s == "cancelled"
               || s == "content_refused" || s == "freed";
    }

    static int Main(string[] args)
    {
        try
        {
            if (args.Length == 3 && args[0] == "write") return Write(args[1], args[2]);
            if (args.Length == 3 && args[0] == "run") return Run(args[1], args[2]);
            Console.Error.WriteLine("usage: write <stateDir> <texts.json> | run <cases.json> <results.json>");
            return 2;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("harness crashed: " + ex);
            return 3;
        }
    }

    static int Write(string stateDir, string textsPath)
    {
        var texts = _js.DeserializeObject(File.ReadAllText(textsPath, Encoding.UTF8)) as object[];
        if (texts == null) return 4;
        foreach (object t in texts)
        {
            var item = new Dictionary<string, object>();
            item["text"] = (string)t;
            item["priority"] = false;
            var items = new List<object>();
            items.Add(item);
            var patch = new Dictionary<string, object>();
            patch["add_goal"] = items;
            if (!FleetCommands.Write(stateDir, patch)) { Console.Error.WriteLine("write failed"); return 5; }
        }
        return 0;
    }

    static string Str(Dictionary<string, object> d, string k)
    {
        object v;
        if (d == null || !d.TryGetValue(k, out v) || v == null) return null;
        return Convert.ToString(v, CultureInfo.InvariantCulture);
    }

    static double Num(Dictionary<string, object> d, string k)
    {
        object v;
        if (d == null || !d.TryGetValue(k, out v) || v == null) return 0.0;
        return Convert.ToDouble(v, CultureInfo.InvariantCulture);
    }

    // status.json the way the cockpit reads it: JavaScriptSerializer, started as a string.
    static void ReadStatus(string path, out string started, out List<Dictionary<string, object>> workers)
    {
        started = "";
        workers = new List<Dictionary<string, object>>();
        if (string.IsNullOrEmpty(path)) return;
        var root = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
        if (root == null) return;
        started = Str(root, "started") ?? "";
        object wo;
        if (root.TryGetValue("workers", out wo) && wo is object[])
            foreach (object o in (object[])wo)
            {
                var d = o as Dictionary<string, object>;
                if (d != null) workers.Add(d);
            }
    }

    static int Run(string casesPath, string resultsPath)
    {
        var cases = _js.DeserializeObject(File.ReadAllText(casesPath, Encoding.UTF8)) as object[];
        var results = new Dictionary<string, object>();
        foreach (object co in cases)
        {
            var c = (Dictionary<string, object>)co;
            string id = Str(c, "id");
            var st = new SubmittedTasks();
            var records = new List<object>();
            string error = null;
            try
            {
                foreach (object so in (object[])c["steps"])
                {
                    var s = (Dictionary<string, object>)so;
                    string op = Str(s, "op");
                    double now = Num(s, "now");
                    string started; List<Dictionary<string, object>> workers;
                    var rec = new Dictionary<string, object>();
                    rec["op"] = op;
                    if (op == "local")
                    {
                        ReadStatus(Str(s, "status"), out started, out workers);
                        rec["ok"] = st.AddLocal(Str(s, "goal"), now, started, workers);
                    }
                    else if (op == "view")
                    {
                        ReadStatus(Str(s, "status"), out started, out workers);
                        string state = Str(s, "state") ?? "";
                        string tasks = Str(s, "tasks") ?? "";
                        List<SubmittedFile> files = SubmittedTasks.ReadFiles(
                            Path.Combine(state, "commands.d"), Path.Combine(tasks, "pending"),
                            Path.Combine(tasks, "for_fleet"));
                        List<SubmittedView> views = st.Refresh(files, started, workers, now);
                        var subs = new List<object>();
                        foreach (SubmittedView v in views)
                        {
                            var d = new Dictionary<string, object>();
                            d["key"] = v.Key; d["goal"] = v.Goal; d["source"] = v.Source;
                            d["id"] = v.Id; d["age_s"] = v.AgeS; d["backed"] = v.Backed;
                            d["unconfirmed"] = v.Unconfirmed; d["stale"] = v.Stale;
                            d["dismissable"] = v.Dismissable;
                            d["label_ja"] = SubmittedTasks.Label(v, true);
                            d["label_en"] = SubmittedTasks.Label(v, false);
                            subs.Add(d);
                        }
                        rec["submitted"] = subs;
                        var order = new List<object>();
                        foreach (SubmittedDisplayItem it in SubmittedTasks.Compose(views, workers, now, IsTerminal))
                            order.Add(it.Submitted != null ? "S:" + it.Submitted.Key : "W:" + Str(it.Worker, "name"));
                        rec["order"] = order;
                        rec["signature_ja"] = SubmittedTasks.Signature(views, true);
                    }
                    else if (op == "dismiss")
                    {
                        rec["ok"] = st.Dismiss(Str(s, "key"), now);
                    }
                    else throw new InvalidOperationException("unknown op " + op);
                    records.Add(rec);
                }
            }
            catch (Exception ex) { error = ex.ToString(); }
            var r = new Dictionary<string, object>();
            r["steps"] = records;
            r["error"] = error;
            results[id] = r;
        }
        File.WriteAllText(resultsPath, _js.Serialize(results), new UTF8Encoding(false));
        return 0;
    }
}
