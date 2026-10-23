// ConvMixHarness.cs -- TEST-ONLY. Drives the SHIPPED ui/FleetConvIdentity.cs functions that decide
// which transcripts belong to which fleet conversation, for
// ui/test_a_fleet_conversation_never_shows_another_conversations_transcripts.py. Not in any Build
// line: see COMPILED_BY_A_TEST in ui/test_one_list_says_what_the_ui_compiles.py.
//
//   ConvMixHarness.exe run <cases.json> <results.json>
//
// ops: registry_match, sync_match, hint_may_join, transcript_guid, stem_of, identity_key.
// Legacy csc, C# 5.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

static class ConvMixHarness
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

    static List<string> StrList(Dictionary<string, object> d, string k)
    {
        var outp = new List<string>();
        object v;
        if (d == null || !d.TryGetValue(k, out v) || v == null) return outp;
        var a = v as object[];
        if (a == null) return outp;
        foreach (object o in a)
            if (o != null) outp.Add(Convert.ToString(o, CultureInfo.InvariantCulture));
        return outp;
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
            object value = null;
            try
            {
                string op = Str(c, "op");
                if (op == "registry_match")
                    value = FleetConvIdentity.RegistryRowMatches(Str(c, "url"), Str(c, "anchor"),
                        Str(c, "row_url"), Str(c, "row_transcript"), StrList(c, "row_transcripts"));
                else if (op == "sync_match")
                    value = FleetConvIdentity.SyncRowMatches(Str(c, "row_url"), Str(c, "row_transcript"),
                        StrList(c, "row_transcripts"), Str(c, "c_url"), Str(c, "c_transcript"),
                        StrList(c, "c_transcripts"));
                else if (op == "hint_may_join")
                    value = FleetConvIdentity.HintMayJoin(Str(c, "url"), ConvListing.TranscriptGuid(Str(c, "hint_path")));
                else if (op == "transcript_guid")
                    value = ConvListing.TranscriptGuid(Str(c, "path"));
                else if (op == "stem_of")
                    value = FleetConvIdentity.StemOf(Str(c, "path"));
                else if (op == "identity_key")
                    value = FleetConvIdentity.IdentityKey(Str(c, "url"), Str(c, "path"));
                else
                    throw new InvalidOperationException("unknown op " + op);
            }
            catch (Exception ex) { error = ex.ToString(); }
            var outp = new Dictionary<string, object>();
            outp["value"] = value;
            outp["error"] = error;
            results[id] = outp;
        }
        File.WriteAllText(resultsPath, _js.Serialize(results), new UTF8Encoding(false));
        return 0;
    }
}
