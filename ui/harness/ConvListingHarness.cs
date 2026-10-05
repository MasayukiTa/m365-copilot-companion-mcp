// ConvListingHarness.cs -- TEST-ONLY. Runs the SHIPPED ConvListing (ui/FleetConvIdentity.cs): the
// transcript index and its paging, the registry read, and the unreadable-history notice that
// ui/CopilotChat.cs's sidebar uses. For ui/test_the_conversation_list_shows_every_transcript.py.
// Not in any Build line: see COMPILED_BY_A_TEST in ui/test_one_list_says_what_the_ui_compiles.py.
//
//   ConvListingHarness.exe pages <transcripts-dir>   -> JSON: [[file names of page 1], [page 2], ...]
//   ConvListingHarness.exe registry <conversations.json> -> JSON: {"count": N, "error": "..."}
//   ConvListingHarness.exe notice <ja|en> <reason>   -> the notice line
//
// "pages" replays what the sidebar does: the first page, then "older" clicks until nothing is
// pending. Legacy csc, C# 5.
using System;
using System.Collections.Generic;
using System.IO;
using System.Web.Script.Serialization;

static class ConvListingHarness
{
    static int Main(string[] args)
    {
        try
        {
            var js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
            if (args.Length == 2 && args[0] == "pages")
            {
                var idx = ConvListing.ListMainTranscripts(args[1]);
                var pages = new List<List<string>>();
                int pos = 0;
                while (pos < idx.Count)
                {
                    int end = ConvListing.PageEnd(idx, pos, ConvListing.MaxPage);
                    if (end <= pos) { Console.Error.WriteLine("PageEnd did not advance"); return 4; }
                    var page = new List<string>();
                    for (int i = pos; i < end; i++) page.Add(Path.GetFileName(idx[i].Key));
                    pages.Add(page);
                    pos = end;
                }
                Console.Out.Write(js.Serialize(pages));
                return 0;
            }
            if (args.Length == 2 && args[0] == "registry")
            {
                string err;
                var rows = ConvListing.ReadRegistry(args[1], out err);
                var d = new Dictionary<string, object>();
                d["count"] = rows.Count;
                d["error"] = err;
                Console.Out.Write(js.Serialize(d));
                return 0;
            }
            if (args.Length == 3 && args[0] == "notice")
            {
                Console.Out.Write(ConvListing.UnreadableNotice(args[1] == "ja", args[2]));
                return 0;
            }
            Console.Error.WriteLine("usage: pages <dir> | registry <file> | notice <ja|en> <reason>");
            return 2;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("harness crashed: " + ex);
            return 3;
        }
    }
}
