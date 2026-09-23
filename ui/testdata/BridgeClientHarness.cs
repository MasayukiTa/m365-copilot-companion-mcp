// BridgeClientHarness.cs -- TEST-ONLY driver for ui/BridgeClient.cs, compiled and run by
// ui/test_the_bridge_client_sends_the_token.py against a throwaway bridge. Not shipped.
//
//   harness call      <out> <base> <pathAndQuery>              one BridgeClient.Call
//   harness twice     <out> <base> <path1> <path2> <gofile>    Call, wait for <gofile>, Call again
//                                                              (same process: the token is cached)
//   harness legacyget <out> <base> <pathAndQuery>              what the OLD window did: a bare GET
//
// Each result is one line in <out> (UTF-8): "OK <body>" or "ERR <status> <message>".
using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;

static class BridgeClientHarness
{
    static string One(string b, string p)
    {
        try { return "OK " + BridgeClient.Call(b, p, 15000).Replace("\n", " "); }
        catch (BridgeClientException ex) { return "ERR " + ex.Status + " " + ex.Message; }
        catch (Exception ex) { return "ERR -1 " + ex.GetType().Name + ": " + ex.Message; }
    }

    static string LegacyGet(string b, string p)
    {
        try
        {
            var req = (HttpWebRequest)WebRequest.Create(b + p);
            req.Proxy = null; req.Timeout = 15000;
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                return "OK " + sr.ReadToEnd();
        }
        catch (Exception ex) { return "ERR -1 " + ex.Message; }
    }

    static int Main(string[] a)
    {
        var lines = new StringBuilder();
        string mode = a[0], outPath = a[1], b = a[2];
        if (mode == "call") lines.AppendLine(One(b, a[3]));
        else if (mode == "legacyget") lines.AppendLine(LegacyGet(b, a[3]));
        else if (mode == "twice")
        {
            lines.AppendLine(One(b, a[3]));
            File.WriteAllText(outPath + ".first", lines.ToString(), new UTF8Encoding(false));
            for (int i = 0; i < 300 && !File.Exists(a[5]); i++) Thread.Sleep(100);
            lines.AppendLine(One(b, a[4]));
        }
        else { Console.Error.WriteLine("unknown mode " + mode); return 2; }
        File.WriteAllText(outPath, lines.ToString(), new UTF8Encoding(false));
        return 0;
    }
}
