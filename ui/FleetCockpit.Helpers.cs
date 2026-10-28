// FleetCockpit.Helpers.cs -- small shared helpers: paths, env, HTTP, JSON field readers, text trims.
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

    static string OneLine(string text, int cap)
    {
        string t = (text ?? "").Replace("\r", " ").Replace("\n", " ").Trim();
        while (t.Contains("  ")) t = t.Replace("  ", " ");
        if (t.Length > cap) t = t.Substring(0, cap) + "…";
        return t;
    }

    static string ResolvePath(string path)
    {
        if (!string.IsNullOrEmpty(path)) return path;
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;          // ...\ui\
        return Path.GetFullPath(Path.Combine(exeDir, "..", ".fleet", "status.json"));
    }

    // ══════════════════════════════════════════════════════════════════════════════════
    //  P0 HEALTH STRIP  —  infra state, glanceable + one-click fixable
    // ══════════════════════════════════════════════════════════════════════════════════
    // Prefer the status file supplied to this process: it lets an isolated/preview cockpit build
    // monitor and repair the selected workspace instead of accidentally acting on the directory
    // that happens to contain the exe. Normal installs still resolve exactly as before because
    // status.json lives in <repo>\.fleet. Fall back to exe\.. for custom/non-.fleet paths.
    string RepoRoot()
    {
        try
        {
            string stateDir = Path.GetDirectoryName(Path.GetFullPath(_statusPath));
            if (string.Equals(Path.GetFileName(stateDir), ".fleet", StringComparison.OrdinalIgnoreCase))
                return Path.GetFullPath(Path.Combine(stateDir, ".."));
        }
        catch (Exception) { }
        return Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
    }

    static bool TruthyField(Dictionary<string, object> d, string key)
    {
        object v;
        if (d == null || !d.TryGetValue(key, out v) || v == null) return false;
        if (v is bool) return (bool)v;
        return string.Equals(Convert.ToString(v), "True", StringComparison.OrdinalIgnoreCase);
    }

    static double NumberField(Dictionary<string, object> d, string key)
    {
        object v;
        if (d == null || !d.TryGetValue(key, out v) || v == null) return 0;
        try { return Convert.ToDouble(v); } catch (Exception) { return 0; }
    }

    static string StringField(Dictionary<string, object> d, string key)
    {
        object v;
        if (d == null || !d.TryGetValue(key, out v) || v == null) return "";
        return Convert.ToString(v);
    }

    // THE LOGIN-WALL PREDICATE IS GONE WITH THE TAB LIST IT READ. It answered "is one of the
    // open tabs a sign-in page", and under the socket route there are no tabs to ask about --
    // the health strip judges sign-in from what the last capture established instead. The
    // failure mode it leaves behind is worth naming: a predicate over an empty list returns
    // false, so the dot went green because nothing was there rather than because sign-in
    // worked. See UpdateCaptureDots.


    // ── .env reader (utf-8, tolerate BOM) ───────────────────────────────────────────
    // The .env lives at the REPO ROOT (one level up from ...\ui), same file doctor.ps1 reads.
    // Cached by mtime so a poll every 15s doesn't re-read from disk each time.
    Dictionary<string, string> _envCache;
    long _envMtime = -1;
    string EnvValue(string key)
    {
        try
        {
            string envPath = Path.Combine(RepoRoot(), ".env");
            if (!File.Exists(envPath)) { _envCache = null; return ""; }
            long m = File.GetLastWriteTimeUtc(envPath).Ticks;
            if (_envCache == null || m != _envMtime)
            {
                var map = new Dictionary<string, string>();
                // UTF8 with BOM tolerated (new UTF8Encoding detects+strips a leading BOM).
                foreach (string raw in File.ReadAllLines(envPath, new UTF8Encoding(false)))
                {
                    string ln = raw;
                    if (ln.Length > 0 && ln[0] == '﻿') ln = ln.Substring(1);   // stray BOM guard
                    ln = ln.Trim();
                    if (ln.Length == 0 || ln[0] == '#') continue;
                    int eq = ln.IndexOf('=');
                    if (eq <= 0) continue;
                    string k = ln.Substring(0, eq).Trim();
                    string v = ln.Substring(eq + 1).Trim();
                    if (k.Length > 0) map[k] = v;
                }
                _envCache = map;
                _envMtime = m;
            }
            string val;
            if (_envCache != null && _envCache.TryGetValue(key, out val)) return val;
        }
        catch (Exception) { }
        return "";
    }

    // Enable TLS 1.2 process-wide: the tunnel is HTTPS and .NET Framework's default
    // protocol set can omit TLS 1.2, so the devtunnels handshake fails (false-red tunnel
    // dot) while plain-HTTP localhost is fine. Called once from the HTTP helpers.
    static bool _tlsReady = false;
    static void EnsureTls()
    {
        if (_tlsReady) return;
        try { ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072; } catch (Exception) { }
        _tlsReady = true;
    }

    // GET a URL; true iff it returns HTTP 200. Short timeout, fully guarded.
    // Loopback URLs bypass the system proxy: on corporate machines the PAC/proxy can
    // swallow 127.0.0.1 requests, turning a healthy local server into a false-red dot.
    // THE BODY WAS ALWAYS THERE AND NOBODY READ IT. /health returns status, auth_fail_10m,
    // tool_ok and fleet_tool_ok (main.py:296-323), and main.py's own docstring says the
    // endpoint is deliberately non-blocking so it answers 200 even while auth is failing and
    // every tool call is dying. HttpOk discards the body, so the Server dot was green in
    // exactly the states the payload was written to expose. Audited 2026-09-16: no consumer
    // of /health anywhere in the repository parsed the body -- not this file, not doctor.ps1,
    // not supervisor.ps1, not the relay.
    //
    // Returns the body on a 200, or null on any failure, so a caller can tell "unreachable"
    // from "reachable and complaining". No JSON parser is pulled in for this: the payload is
    // flat and the two questions asked of it are substring-shaped.
    static string HttpBody(string url, int timeoutMs)
    {
        try
        {
            EnsureTls();
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "GET";
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.AllowAutoRedirect = true;
            if (url.Contains("127.0.0.1") || url.Contains("localhost")) req.Proxy = null;
            else if (req.Proxy != null) req.Proxy.Credentials = CredentialCache.DefaultCredentials;
            using (var resp = (HttpWebResponse)req.GetResponse())
            {
                if (resp.StatusCode != HttpStatusCode.OK) return null;
                using (var sr = new StreamReader(resp.GetResponseStream()))
                    return sr.ReadToEnd();
            }
        }
        catch (Exception) { return null; }
    }

    // Pull one flat JSON value out without a parser. Returns "" when absent, which every
    // caller must treat as "no evidence" rather than as a negative -- /health omits fields it
    // has nothing to say about, and an omission is not a failure.
    static string HealthField(string body, string key)
    {
        if (string.IsNullOrEmpty(body)) return "";
        int i = body.IndexOf("\"" + key + "\"");
        if (i < 0) return "";
        int c = body.IndexOf(':', i);
        if (c < 0) return "";
        int e = c + 1;
        while (e < body.Length && body[e] != ',' && body[e] != '}') e++;
        return body.Substring(c + 1, e - c - 1).Trim().Trim('"');
    }

    static bool HttpOk(string url, int timeoutMs)
    {
        try
        {
            EnsureTls();
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "GET";
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.AllowAutoRedirect = true;
            if (url.Contains("127.0.0.1") || url.Contains("localhost")) req.Proxy = null;
            else if (req.Proxy != null) req.Proxy.Credentials = CredentialCache.DefaultCredentials;
            using (var resp = (HttpWebResponse)req.GetResponse())
                return resp.StatusCode == HttpStatusCode.OK;
        }
        catch (Exception) { return false; }
    }

    // GET a URL; return the body string, or null on any failure (unreachable / non-200 / timeout).
    static string HttpGetBody(string url, int timeoutMs)
    {
        try
        {
            EnsureTls();
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "GET";
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            if (url.Contains("127.0.0.1") || url.Contains("localhost")) req.Proxy = null;
            using (var resp = (HttpWebResponse)req.GetResponse())
            {
                if (resp.StatusCode != HttpStatusCode.OK) return null;
                using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                    return sr.ReadToEnd();
            }
        }
        catch (Exception) { return null; }
    }

    static string S(Dictionary<string, object> d, string k)
    { if (d != null && d.ContainsKey(k) && d[k] != null) return d[k].ToString(); return ""; }

    // Cheap deterministic FNV-1a 32-bit hash over a string. Used for RowSig so two strings
    // of the same length but different content produce different signatures, forcing a re-render.
    static int StableShortHash(string s)
    {
        if (s == null || s.Length == 0) return 0;
        unchecked
        {
            int hash = (int)2166136261u;
            for (int i = 0; i < s.Length; i++)
                hash = (hash ^ (int)s[i]) * 16777619;
            return hash;
        }
    }

    static int I(Dictionary<string, object> d, string k)
    { try { if (d != null && d.ContainsKey(k) && d[k] != null) return Convert.ToInt32(d[k]); } catch (Exception) { } return 0; }
    static double Dbl(Dictionary<string, object> d, string k)
    { try { if (d != null && d.ContainsKey(k) && d[k] != null) return Convert.ToDouble(d[k]); } catch (Exception) { } return 0; }

    static Dictionary<string, object> Obj(Dictionary<string, object> d, string k)
    {
        if (d == null || !d.ContainsKey(k) || d[k] == null) return null;
        return d[k] as Dictionary<string, object>;
    }

    static int ArrCount(Dictionary<string, object> d, string k)
    {
        if (d == null || !d.ContainsKey(k) || d[k] == null) return 0;
        var a = d[k] as object[];
        return a != null ? a.Length : 0;
    }

    string Sig(Dictionary<string, object> root)
    {
        var sb = new StringBuilder();
        sb.Append(_dark ? "D" : "L").Append(_lang).Append('|');
        sb.Append(_stopping ? "st1" : "st0").Append('|');   // FIX B: force a re-render when Stop toggles the dim
        sb.Append("h").Append(_history.Count).Append('|');
        sb.Append(S(root, "done_count")).Append('/').Append(S(root, "total")).Append('|');
        object wo;
        if (root.TryGetValue("workers", out wo) && wo is object[])
            foreach (object o in (object[])wo)
            {
                var w = (Dictionary<string, object>)o;
                string nm = S(w, "name");
                sb.Append(nm).Append(S(w, "status")).Append(S(w, "turn"));
                // only an EXPANDED card shows live progress text, so only its `last` hash
                // changes need to force a re-render. Collapsed cards stay put while their
                // worker streams -- that's what keeps a 164-task fleet from thrashing.
                if (_expanded.Contains(nm)) sb.Append('#').Append(StableShortHash(S(w, "last")));
                var ex = Obj(w, "execution");
                if (ex != null)
                    sb.Append("|x").Append(S(ex, "state"))
                      .Append(':').Append(S(ex, "current_step_index"))
                      .Append(':').Append(S(ex, "completed_count"))
                      .Append(':').Append(StableShortHash(S(ex, "last_progress")))
                      .Append(':').Append(S(ex, "last_progress_at"));
                sb.Append(';');
            }
        return sb.ToString();
    }

    string Trunc(string s, int n)
    {
        s = (s ?? "").Trim();
        return s.Length <= n ? s : s.Substring(0, n - 1) + "…";
    }
}
