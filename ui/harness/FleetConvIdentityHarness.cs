// FleetConvIdentityHarness.cs -- TEST-ONLY. Drives the SHIPPED ui/FleetConvIdentity.cs the same
// way ui/CopilotChat.cs's OpenFromFleet() and SyncRegistry() do, then feeds the resulting
// Conversation into the SHIPPED ui/ChatSend.cs decision (LiveWorkerFor + DecideFleetSend) so the
// test can see whether the merged identity actually keeps a fleet conversation answerable --
// not just that some field got assigned. For
// ui/test_a_fleet_interrupt_survives_a_supervisor_restart.py. Not in any Build line: see
// COMPILED_BY_A_TEST in ui/test_one_list_says_what_the_ui_compiles.py.
//
//   FleetConvIdentityHarness.exe run <cases.json> <results.json>
//
// Each case names an "op":
//   "open"              -- replays OpenFromFleet's own three calls, in the same order it makes
//                           them: ResolveGoal, then MergeBackfillOnly(Source)/MergeBackfillOnly
//                           (Name)/MergeForward(Transcript)/MergeForward(Goal).
//   "registry_backfill" -- replays SyncRegistry's backfill: MergeBackfillOnly(Goal) and
//                           MergeBackfillOnly(Transcript) only (Source/Name are untouched by
//                           SyncRegistry -- a registry row already carries its own).
//
// The resulting Conversation is then run through ChatSend.LiveWorkerFor + DecideFleetSend, the
// SAME functions ui/CopilotChat.cs's SendToFleetConversation uses, to answer the question that
// actually matters: does a follow-up/interrupt typed into this conversation get delivered, or
// refused fleet_no_goal?
//
// Legacy csc, C# 5.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

static class FleetConvIdentityHarness
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

    static string Str(Dictionary<string, object> d, string k)
    {
        object v;
        if (d == null || !d.TryGetValue(k, out v) || v == null) return "";
        return Convert.ToString(v, CultureInfo.InvariantCulture);
    }

    static int IntOr(Dictionary<string, object> d, string k, int dflt)
    {
        object v;
        if (d == null || !d.TryGetValue(k, out v) || v == null) return dflt;
        return Convert.ToInt32(v, CultureInfo.InvariantCulture);
    }

    static int Run(string casesPath, string resultsPath)
    {
        var cases = _js.DeserializeObject(File.ReadAllText(casesPath, Encoding.UTF8)) as object[];
        var results = new Dictionary<string, object>();
        foreach (object co in cases)
        {
            var c = (Dictionary<string, object>)co;
            string id = Str(c, "id");
            string error = null;
            var outp = new Dictionary<string, object>();
            try
            {
                string op = Str(c, "op");
                var conv = new Conversation();
                conv.Source = Str(c, "existing_source");
                conv.Name = Str(c, "existing_name");
                conv.Transcript = Str(c, "existing_transcript");
                conv.Goal = Str(c, "existing_goal");
                conv.ConvUrl = Str(c, "conv_url");

                string resolvedGoal;
                if (op == "open")
                {
                    // EXACT REPLAY of OpenFromFleet's own sequence (ui/CopilotChat.cs):
                    //   bestGoal = FleetConvIdentity.ResolveGoal(liveGoal, TranscriptMetaGoal(...));
                    //   c.Source = FleetConvIdentity.MergeBackfillOnly(c.Source, "fleet");
                    //   c.Name = FleetConvIdentity.MergeBackfillOnly(c.Name, worker);
                    //   c.Transcript = FleetConvIdentity.MergeForward(c.Transcript, transcriptPath);
                    //   c.Goal = FleetConvIdentity.MergeForward(c.Goal, bestGoal);
                    // transcript_meta_goal stands in for TranscriptMetaGoal(transcriptPath)'s
                    // result -- that helper is a plain first-line JSON read with its own
                    // coverage in the source-text test; what matters here is what
                    // FleetConvIdentity does with whatever it returned.
                    string liveGoal = Str(c, "live_goal");
                    string metaGoal = Str(c, "transcript_meta_goal");
                    resolvedGoal = FleetConvIdentity.ResolveGoal(liveGoal, metaGoal);
                    string worker = Str(c, "worker");
                    string transcriptPath = Str(c, "transcript_path");
                    conv.Source = FleetConvIdentity.MergeBackfillOnly(conv.Source, "fleet");
                    conv.Name = FleetConvIdentity.MergeBackfillOnly(conv.Name, worker);
                    conv.Transcript = FleetConvIdentity.MergeForward(conv.Transcript, transcriptPath);
                    conv.Goal = FleetConvIdentity.MergeForward(conv.Goal, resolvedGoal);
                }
                else if (op == "registry_backfill")
                {
                    // EXACT REPLAY of SyncRegistry's backfill branch (ui/CopilotChat.cs):
                    //   existingC.Goal = FleetConvIdentity.MergeBackfillOnly(existingC.Goal, regGoal);
                    //   existingC.Transcript = FleetConvIdentity.MergeBackfillOnly(existingC.Transcript, regTranscript);
                    string regGoal = Str(c, "reg_goal");
                    string regTranscript = Str(c, "reg_transcript");
                    resolvedGoal = regGoal;
                    conv.Goal = FleetConvIdentity.MergeBackfillOnly(conv.Goal, regGoal);
                    conv.Transcript = FleetConvIdentity.MergeBackfillOnly(conv.Transcript, regTranscript);
                }
                else
                {
                    throw new InvalidOperationException("unknown op " + op);
                }

                string statusText = Str(c, "status_text");
                string live = ChatSend.LiveWorkerFor(statusText, conv.Transcript);
                string text = Str(c, "text");
                int lang = IntOr(c, "lang", 1);
                ChatSend.FleetSend fs = ChatSend.DecideFleetSend(text, live, conv, lang);

                outp["resolved_goal"] = resolvedGoal;
                outp["goal"] = conv.Goal;
                outp["transcript"] = conv.Transcript;
                outp["source"] = conv.Source;
                outp["name"] = conv.Name;
                outp["live"] = live;
                var fsend = new Dictionary<string, object>();
                fsend["kind"] = fs.Kind;
                fsend["refusal_key"] = fs.RefusalKey;
                fsend["key"] = fs.Key;
                fsend["item"] = fs.Item;
                outp["fleet_send"] = fsend;
            }
            catch (Exception ex) { error = ex.ToString(); }
            outp["error"] = error;
            results[id] = outp;
        }
        File.WriteAllText(resultsPath, _js.Serialize(results), new UTF8Encoding(false));
        return 0;
    }
}
