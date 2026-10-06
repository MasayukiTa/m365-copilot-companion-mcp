// BridgeClient.cs -- the ONE way the chat window talks to the bridge (bridge/copilot_bridge.py).
//
// WHY THIS EXISTS (2026-09-24). Every bridge endpoint used to be a bare GET with no check, so any
// web page open in any browser on this machine -- or any other local process -- could start a
// Copilot turn, run a /goal or delete a conversation through the signed-in session. The bridge
// now demands, on everything that changes state or reads the page:
//
//   * POST, with the old query fields as an application/x-www-form-urlencoded body, and
//   * X-Bridge-Token: the per-start token the bridge writes to an owner-only file under
//     %LOCALAPPDATA%\m365-copilot-companion\bridge\token-<port> (bridge/bridge_auth.py).
//
// A BRIDGE RESTART ROTATES THE TOKEN, so the cached copy is re-read once on a 401 before the
// call is reported as refused: a window left open across a restart keeps working.
//
// DEPLOY ORDER FAILS LOUDLY, NOT SILENTLY. This window against an OLD bridge: the old bridge
// writes no token file (-> "no token file" message) or answers POST with 501 (-> "the bridge is
// older than this window"). Both are BridgeClientException, whose message says what to do.
//
// Kept free of WPF so ui/test_the_bridge_client_sends_the_token.py can compile it with csc and
// run it against a real bridge Handler on a throwaway port.
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;

public class BridgeClientException : Exception
{
    public readonly int Status;   // HTTP status of the refusal; 0 when there was no response
    public BridgeClientException(string message, int status, Exception inner)
        : base(message, inner) { Status = status; }
}

public static class BridgeClient
{
    public const string TokenHeader = "X-Bridge-Token";
    public const string TokenDirEnv = "MCP_BRIDGE_TOKEN_DIR";

    static readonly object _lock = new object();
    static readonly Dictionary<string, string> _cache = new Dictionary<string, string>();

    /// The token file for the bridge at `baseUrl` -- the same rule as bridge_auth.token_path.
    public static string TokenPath(string baseUrl)
    {
        string dir = Environment.GetEnvironmentVariable(TokenDirEnv);
        if (string.IsNullOrEmpty(dir) || dir.Trim().Length == 0)
        {
            string root = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            if (string.IsNullOrEmpty(root))
                root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                                    Path.Combine(".local", "share"));
            dir = Path.Combine(root, Path.Combine("m365-copilot-companion", "bridge"));
        }
        return Path.Combine(dir.Trim(), "token-" + new Uri(baseUrl).Port);
    }

    /// The token, from the cache unless `reload`; null when the file is missing or empty.
    public static string Token(string baseUrl, bool reload)
    {
        string p = TokenPath(baseUrl);
        lock (_lock)
        {
            string t;
            if (!reload && _cache.TryGetValue(p, out t)) return t;
            try { t = File.ReadAllText(p, Encoding.ASCII).Trim(); } catch { t = null; }
            if (string.IsNullOrEmpty(t)) { _cache.Remove(p); return null; }
            _cache[p] = t;
            return t;
        }
    }

    /// Split "/path?a=b" into ("/path", "a=b"). The query becomes the POST body.
    public static void SplitPath(string pathAndQuery, out string path, out string form)
    {
        int q = pathAndQuery.IndexOf('?');
        path = q < 0 ? pathAndQuery : pathAndQuery.Substring(0, q);
        form = q < 0 ? "" : pathAndQuery.Substring(q + 1);
    }

    /// One authenticated POST. `onRequest` sees every HttpWebRequest made (a 401 retry makes a
    /// second one), so a caller can keep it to Abort() -- the chat's Stop button does. Protocol
    /// refusals become BridgeClientException; everything else (connection refused, a timeout, an
    /// Abort) propagates unchanged, so callers can still tell "down" from "cancelled".
    public static HttpWebResponse Open(string baseUrl, string pathAndQuery, int timeoutMs,
                                       Action<HttpWebRequest> onRequest)
    {
        string path, form;
        SplitPath(pathAndQuery, out path, out form);
        byte[] body = Encoding.ASCII.GetBytes(form);
        string used = null;
        for (int attempt = 0; attempt < 2; attempt++)
        {
            string tok = Token(baseUrl, attempt > 0);
            if (tok == null)
                throw new BridgeClientException(
                    "No bridge token at " + TokenPath(baseUrl) + ". Either the bridge at " + baseUrl +
                    " is not running, or it is older than this window (an old bridge writes no " +
                    "token). Restart the bridge: stop the python process running " +
                    "bridge/copilot_bridge.py and let the supervisor bring it back, or run " +
                    "start_all.bat.", 0, null);
            if (attempt > 0 && tok == used) break;   // nothing new to try
            used = tok;
            var req = (HttpWebRequest)WebRequest.Create(baseUrl + path);
            req.Method = "POST";
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.Proxy = null;          // loopback: never through a proxy, never hand it the token
            req.AllowAutoRedirect = false;
            // No "Expect: 100-continue": the bridge speaks HTTP/1.0 and never sends a 100, so
            // .NET would stall every call for its 350 ms continue timeout before the body.
            req.ServicePoint.Expect100Continue = false;
            req.ContentType = "application/x-www-form-urlencoded";
            req.Headers[TokenHeader] = tok;
            req.ContentLength = body.Length;
            if (onRequest != null) onRequest(req);
            try
            {
                using (var s = req.GetRequestStream()) s.Write(body, 0, body.Length);
                return (HttpWebResponse)req.GetResponse();
            }
            catch (WebException wex)
            {
                var r = wex.Response as HttpWebResponse;
                if (r == null)
                {
                    // AN OLD BRIDGE OFTEN ANSWERS WITH A RESET, NOT A 501: it never reads the POST
                    // body, and a socket closed with unread bytes is reset by Windows, so the 501
                    // that would explain it is lost. Ask /status, which every bridge answers to a
                    // GET: one without "authenticated" predates the token. Never for a Stop.
                    if (wex.Status != WebExceptionStatus.RequestCanceled && IsOldBridge(baseUrl))
                        throw new BridgeClientException(Describe(baseUrl, path, 501, "(connection reset)", ""), 501, wex);
                    throw;
                }
                int code = (int)r.StatusCode;
                string reason = r.StatusDescription;
                string text = "";
                try { using (var sr = new StreamReader(r.GetResponseStream(), Encoding.UTF8)) text = sr.ReadToEnd(); }
                catch { }
                r.Close();
                if (code == 401 && attempt == 0) continue;   // restarted bridge: re-read the token
                throw new BridgeClientException(Describe(baseUrl, path, code, reason, text), code, wex);
            }
        }
        throw new BridgeClientException(Describe(baseUrl, path, 401, "Unauthorized", ""), 401, null);
    }

    /// One authenticated POST, the whole body as text.
    public static string Call(string baseUrl, string pathAndQuery, int timeoutMs)
    {
        using (var resp = Open(baseUrl, pathAndQuery, timeoutMs, null))
        using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
            return sr.ReadToEnd();
    }

    /// True when something answers GET /status as a bridge that predates the token.
    public static bool IsOldBridge(string baseUrl)
    {
        try
        {
            var req = (HttpWebRequest)WebRequest.Create(baseUrl + "/status");
            req.Method = "GET"; req.Proxy = null; req.Timeout = 5000; req.ReadWriteTimeout = 5000;
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
            {
                string s = sr.ReadToEnd();
                return s.TrimStart().StartsWith("{") && s.Contains("\"ok\"") && !s.Contains("\"authenticated\"");
            }
        }
        catch { return false; }
    }

    public static string Describe(string baseUrl, string path, int code, string reason, string body)
    {
        string head = "The bridge at " + baseUrl + " refused " + path + " (HTTP " + code +
                      (string.IsNullOrEmpty(reason) ? "" : " " + reason) + ")";
        if (code == 501 || code == 405)
            return head + ": the bridge is older than this window and does not accept " +
                   "authenticated POST requests. Restart the bridge (stop the python process " +
                   "running bridge/copilot_bridge.py; the supervisor restarts it with the current code).";
        if (code == 401)
            return head + ": the token in " + TokenPath(baseUrl) + " was not accepted even after " +
                   "re-reading it. The bridge may have restarted without writing a token -- see " +
                   ".setup/logs/bridge.log.";
        if (!string.IsNullOrEmpty(body))
            return head + ": " + (body.Length > 300 ? body.Substring(0, 300) : body);
        return head + ".";
    }
}
