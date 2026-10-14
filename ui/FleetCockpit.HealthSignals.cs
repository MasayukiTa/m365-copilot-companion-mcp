// FleetCockpit.HealthSignals.cs -- capture/route/tool-probe dots, agent marker and the readers behind them.
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

    // Sign-in and Agent, from what the last capture established rather than from tabs.
    //
    //   sign-in   grey    no capture on record and no run wanting one
    //             green   the last capture worked and its token still has life
    //             amber   the last capture failed for a reason a person cannot fix, or the
    //                     token has expired while a run is live
    //             red     the last capture failed because sign-in is needed
    //
    //   agent     grey    no run
    //             green   run live, the route is open and the template names an agent
    //             amber   run live and the route is CLOSED -- every worker is on a tab,
    //                     which works and costs several times more. Not an error, and not
    //                     invisible either: amber is exactly 'working, worth knowing'.
    //             red     run live and no agent was ever established
    void UpdateCaptureDots(DateTime now)
    {
        Dictionary<string, object> cap = ReadCaptureStatus();
        bool live = RunIsLive();

        if (cap == null)
        {
            // NO RECORD. Grey while idle -- a system that has not captured yet is not
            // unhealthy. Amber during a run, because then the evidence IS expected.
            SetDot(3, live ? HealthState.Yellow : HealthState.Gray,
                   T(live ? "hs_signin_unknown_live" : "hs_signin_gray"), now);
            // AMBER FIRST, by this file's own rule two lines up: evidence that is expected
            // and missing is not evidence of failure. The sign-in dot gets amber in exactly
            // this situation on the line above; the agent dot jumped to red, and red here
            // drives a reconnect. A first run on a fresh checkout has no capture record at
            // all, and neither does one where the status writer swallowed an exception.
            SetDot(4, live ? HealthState.Yellow : HealthState.Gray,
                   T(live ? "hs_agent_unknown_live" : "hs_agent_gray"), now);
            return;
        }

        bool ok = TruthyField(cap, "ok");
        double expiresAt = NumberField(cap, "expires_at");
        string kind = StringField(cap, "kind");
        // THE FIELD IS `at`. relay/capture_status.py's FIELDS tuple is ("ok", "at",
        // "expires_at", ...) and there has never been a "ts" in it -- so capTs was 0 on every
        // read, capFresh below was false on every read, and the RED branch that needs it has
        // never once been taken. The sign-in dot was structurally incapable of turning red:
        // a capture that failed for a sign-in reason five seconds ago showed the same amber
        // as one that failed last week, which is precisely the distinction the comment below
        // spends a paragraph arguing for.
        //
        // `ts` is still read as a fallback so a file written by any other producer, or an
        // older one, keeps working.
        double capTs = NumberField(cap, "at");
        if (capTs <= 0) capTs = NumberField(cap, "ts");
        string gptId = StringField(cap, "gpt_id");
        double nowEpoch = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        double lifeLeft = expiresAt - nowEpoch;

        // SIGN-IN. A live token is positive evidence. An expired one is NOT automatically
        // red: the next capture may well succeed, and a dot that stays red for hours after a
        // finished run is a dot people stop reading. Expiry only matters while a run wants
        // a token.
        // AND IT HAS TO BE RECENT. capture_status.json keeps the last capture whether it was a
        // minute or a day ago, so an old sign-in failure stayed red indefinitely -- driving a
        // repair that relaunches the browser HEADED, for a refusal that may long since have
        // resolved. Evidence about the past is not evidence about now.
        double capAgeS = (capTs > 0) ? (nowEpoch - capTs) : -1;
        // HOW LONG A BROKEN SIGN-IN MAY GO UNNOTICED IS OUR REQUIREMENT, NOT THE ISSUER'S.
        // Two wrong answers were tried here before this one.
        //
        // A flat 30 minutes was never measured against anything. The token on disk when this
        // was written lived 3,848s (at=1789532602, expires_at=1789536451, about 64 minutes)
        // and captures happen roughly once per token lifetime, so the back half of EVERY
        // healthy cycle showed amber "unverified" with nothing wrong -- and a dot that is
        // amber through half of normal operation is one people stop reading, which is the
        // failure this file argues against repeatedly, reached from the other side.
        //
        // Then the window was taken from the record as (expires_at - at) and called "one
        // capture cycle". It is not one: now - at <= expires_at - at reduces to
        // now <= expires_at, which is "green while the token has not expired" -- redundant
        // with the lifeLeft > 0 already on this branch, and worse, it hands the detection
        // delay to whoever issues the tokens. If their lifetime became 24 hours, a capture
        // path that died would stay green for 24 hours and nothing here would have changed.
        //
        // So: a constant, because the requirement is ours -- but one with a measurement under
        // it. 5,400s is 1.4 token lifetimes as measured on this machine, which leaves room
        // for a capture that comes slightly late without keeping green alive across a whole
        // missed cycle. Expiry is a separate and additional ceiling (lifeLeft > 0 below), so
        // green needs both: a token that has not run out AND evidence no older than this.
        // Neither establishes that the session was not revoked in between; nothing in a
        // record of past captures can.
        bool capFresh = capAgeS >= 0 && capAgeS <= SIGNIN_EVIDENCE_MAX_AGE_S;
        if (!ok && string.Equals(kind, "signin", StringComparison.OrdinalIgnoreCase) && capFresh)
            SetDot(3, HealthState.Red, T("hs_signin_bad"), now);
        else if (!ok && string.Equals(kind, "signin", StringComparison.OrdinalIgnoreCase))
            SetDot(3, HealthState.Yellow, T("hs_signin_old"), now);
        else if (!ok)
            SetDot(3, HealthState.Yellow, T("hs_signin_failed_other"), now);
        else if (lifeLeft > 0 && capFresh)
            SetDot(3, HealthState.Green, T("hs_signin_ok"), now);
        else if (lifeLeft > 0)
            // THE RULE THE PARAGRAPH ABOVE ARGUES FOR, APPLIED TO SUCCESSES TOO. "Evidence
            // about the past is not evidence about now" was written for failures and only
            // ever gated failures: an ok=true capture with a far-future expires_at read as
            // green forever, however old the record, because nothing re-verifies the session.
            // A token can be revoked by tenant policy or a sign-out elsewhere and this dot
            // would not notice until some later capture happened to fail.
            //
            // Amber while a run is live, because then the evidence IS expected and its
            // absence matters. Grey when idle, because nothing is asking and an old record is
            // simply not current evidence -- painting an idle machine amber would be the
            // false-alarm half of the same mistake.
            SetDot(3, live ? HealthState.Yellow : HealthState.Gray,
                   T(live ? "hs_signin_old_ok" : "hs_signin_gray_old"), now);
        else if (live)
            SetDot(3, HealthState.Yellow, T("hs_signin_stale"), now);
        else
            SetDot(3, HealthState.Gray, T("hs_signin_gray_expired"), now);

        // AGENT. The template naming an agent is a STRONGER guarantee than the tab sniffing
        // it replaces: a capture whose request names no agent raises NotAnAgentSurface and
        // produces no template at all, so a silent bind to the default Copilot -- no
        // connectors, no tenant grounding, and a fluent answer -- cannot happen unnoticed.
        if (!live)
            SetDot(4, HealthState.Gray, T("hs_agent_gray"), now);
        else if (RouteState() == ROUTE_UNKNOWN)
            // Amber, not green: the route's own record could not be read, so whether workers
            // are on tabs is unknown, and the binding check below would be answering a
            // different question than the one being asked.
            SetDot(4, HealthState.Yellow, T("hs_agent_route_unknown"), now);
        else if (RouteState() == ROUTE_CLOSED)
        {
            // THE CANNED-ANSWER SNIFF SURVIVES HERE AND NOWHERE ELSE. It used to decide the
            // dot's colour, which made a judgement about ONE TURN'S QUALITY into a statement
            // about infrastructure. It still means something while workers are on tabs, so it
            // is demoted to a note on the amber rather than deleted.
            string tail = NewestAssistantText();
            string note = T("hs_agent_tabs");
            if (tail != null && LooksLikeCannedNonAnswer(tail)) note += T("hs_agent_canned");
            SetDot(4, HealthState.Yellow, note, now);
        }
        else if (FleetAgentIsBound())
            SetDot(4, HealthState.Green, T("hs_agent_ok"), now);
        else if (!string.IsNullOrEmpty(gptId))
            // THIS FALLBACK IS THE FIELD THE CHECK ABOVE EXISTS TO DISTRUST. Read
            // FleetAgentIsBound's own comment: capture_status.json's gpt_id is "the LAST
            // capture on ANY surface", the route also captures for side agents, and "the last
            // event about somebody else is not evidence about you". Then this line took that
            // same field as sufficient for GREEN, reintroducing precisely what the primary
            // check was built to avoid.
            //
            // It is kept, because a non-empty gpt_id is not nothing -- some surface did bind
            // to some agent. It is amber, because it is not evidence about THIS one.
            SetDot(4, HealthState.Yellow, T("hs_agent_other_surface"), now);
        else
            SetDot(4, HealthState.Red, T("hs_agent_bad"), now);
    }

    // Is the FLEET'S OWN surface bound to an agent?
    //
    // NOT capture_status.json's gpt_id, which is the LAST capture on ANY surface. The route
    // also captures for side agents -- a researcher or an analyst, on conversation-specific
    // URLs -- and one of those was observed writing an empty gpt_id, which would have turned
    // this dot red while the fleet's own agent was working perfectly. The last event about
    // somebody else is not evidence about you.
    //
    // The per-surface fact is already on disk: relay/profile_token.py caches one request
    // template per agent surface, named by a hash of its URL, and refuses to return one that
    // names no agent. Its existence IS the binding.
    bool FleetAgentIsBound()
    {
        try
        {
            string url = EnvValue("MCP_FLEET_AGENT_URL");
            if (string.IsNullOrEmpty(url)) url = EnvValue("MCP_IMPL_AGENT_URL");
            if (string.IsNullOrEmpty(url)) return false;
            string path = Path.Combine(RepoRoot(), ".fleet", "templates",
                                       "template_" + Sha256Prefix(url) + ".json");
            if (!File.Exists(path)) return false;
            // EXISTENCE IS NOT THE BINDING, and the comment above used to say it was. The
            // module that writes this file refuses to return one older than
            // TEMPLATE_MAX_AGE_S (relay/profile_token.py), and refuses one whose gpt_id is
            // empty (load_template's "a template without an agent is not a cache hit"). This
            // dot checked neither, so it could report a binding the route itself would evict
            // on first use -- and would go on reporting it, because the eviction only happens
            // when a capture runs, about once per token lifetime.
            //
            // Measured when this was written: .fleet/templates held one template 211.7 hours
            // old, 8.8 days against a 24 hour cap. Dot 3 was given an explicit max age for
            // exactly this reason ("evidence about the past is not evidence about now"); dot
            // 4 was not.
            var tpl = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8))
                      as Dictionary<string, object>;
            if (tpl == null) return false;
            double savedAt = NumberField(tpl, "ts");
            double ageS = NowUnix() - savedAt;
            if (savedAt <= 0 || ageS > TEMPLATE_MAX_AGE_S) return false;
            var query = tpl.ContainsKey("query") ? tpl["query"] as Dictionary<string, object>
                                                 : null;
            // Non-EMPTY, not merely present. The old substring test asked whether the five
            // characters "gptId" appeared, which is a question about the file's spelling.
            return !string.IsNullOrEmpty(StringField(query, "gptId"));
        }
        catch (Exception) { return false; }
    }

    // The same 16 hex characters relay/profile_token.py names its cache files with. If these
    // two ever disagree the lookup finds nothing and the dot reports "not bound" for ever --
    // a silent zero -- so the algorithm is stated in both places rather than assumed.
    //: A COPY OF relay/profile_token.py's TEMPLATE_MAX_AGE_S, and copies drift, so a test
    //: fails if the two stop agreeing. Stated here rather than read from the environment
    //: because the cockpit must not report a binding on terms looser than the route's.
    const double TEMPLATE_MAX_AGE_S = 24 * 3600;

    static string Sha256Prefix(string s)
    {
        using (var sha = System.Security.Cryptography.SHA256.Create())
        {
            byte[] hash = sha.ComputeHash(System.Text.Encoding.UTF8.GetBytes(s ?? ""));
            var sb = new StringBuilder();
            for (int i = 0; i < 8; i++) sb.Append(hash[i].ToString("x2"));
            return sb.ToString();
        }
    }

    Dictionary<string, object> ReadCaptureStatus()
    {
        try
        {
            string path = Path.Combine(RepoRoot(), ".fleet", "capture_status.json");
            if (!File.Exists(path)) return null;
            return _js.DeserializeObject(File.ReadAllText(path)) as Dictionary<string, object>;
        }
        catch (Exception) { return null; }
    }

    // Has the route's one-way breaker tripped DURING THIS RUN? Read from the route's own
    // append-only record. Scanned from the run's start, because a close is per-run: the route
    // is rebuilt with each coordinator, and a close from yesterday says nothing about now.
    //: What RouteState() found. UNKNOWN exists because the alternative is a check that
    //: reports a healthy open route when it could not read the file at all.
    const int ROUTE_OPEN = 0;
    const int ROUTE_CLOSED = 1;
    const int ROUTE_UNKNOWN = 2;

    int RouteState()
    {
        try
        {
            string path = Path.Combine(RepoRoot(), ".fleet", "socket_route.jsonl");
            // NO FILE IS A REAL ANSWER, not a failure to read one: nothing has ever closed
            // the route on this checkout. Only a file that exists and cannot be understood
            // is unknown.
            if (!File.Exists(path)) return ROUTE_OPEN;
            string stamp = RunStartedLocal().ToString("yyyy-MM-ddTHH:mm:ss");
            // THE LAST ROUTE EVENT DECIDES, NOT THE FIRST CLOSE. A closed route can now be
            // reopened mid-run (relay/route_reopen.py), and this used to answer "closed" if
            // it found any close at all since the run began -- so a route that came back
            // after a passing fault stayed amber, and the dot described a state the fleet
            // had already left. Both events carry the same timestamp key, so the scan just
            // keeps the newest one it sees.
            bool closed = false;
            int routeLines = 0, parsed = 0;
            foreach (string line in File.ReadLines(path))
            {
                bool isClose = line.IndexOf("route_closed", StringComparison.Ordinal) >= 0;
                bool isOpen = line.IndexOf("route_reopened", StringComparison.Ordinal) >= 0;
                if (!isClose && !isOpen) continue;
                routeLines++;
                // The record is written by json.dumps, which puts a space after the colon:
                //   {"ts": 1787270671.84, "at": "2026-08-21T09:04:31", "event": "route_closed"}
                // Matching without the space finds nothing, and finding nothing here means the
                // dot reports an open route for ever -- a silent zero, which is how a check
                // fails open. The marker is taken from a real line rather than composed.
                const string AtKey = "\"at\": \"";
                int i = line.IndexOf(AtKey, StringComparison.Ordinal);
                if (i < 0) continue;
                int from = i + AtKey.Length;
                if (from + 19 > line.Length) continue;
                string at = line.Substring(from, 19);
                parsed++;
                if (string.CompareOrdinal(at, stamp) >= 0) closed = isClose;
            }
            // ROUTE EVENTS THAT NONE OF THIS COULD READ. The comment above explains why the
            // marker is copied from a real line rather than composed; this is what happens
            // when that copy stops matching anyway. Saying "open" there is the silent zero
            // the comment warns about, stated and then not guarded against.
            if (routeLines > 0 && parsed == 0) return ROUTE_UNKNOWN;
            return closed ? ROUTE_CLOSED : ROUTE_OPEN;
        }
        catch (Exception) { }
        // An unreadable file is not an open route. It is nothing at all, and the dot now has
        // somewhere to put that.
        return ROUTE_UNKNOWN;
    }

    DateTime RunStartedLocal()
    {
        try
        {
            double started = NumberField(ReadStatus(), "started");
            if (started > 0)
                return new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(started).ToLocalTime();
        }
        catch (Exception) { }
        return DateTime.Now.AddHours(-24);
    }
    // A COPY OF relay/relay_fleet.py's CANNED_NONANSWER_MARKERS, and copies drift. The
    // escalation line was in a real run for hours before either list had it; a test now
    // fails if the two stop agreeing, because a cockpit that cannot recognise what the
    // relay treats as a non-answer shows a worker as healthy while the relay re-queues it.
    static readonly string[] _cannedNonAnswer = {
        "それに応答できませんでした", "I couldn't respond to that", "I can't respond to that",
        "担当者へのエスカレーションが構成されていません",
    };
    static bool LooksLikeCannedNonAnswer(string s)
    {
        if (string.IsNullOrEmpty(s)) return false;
        foreach (string m in _cannedNonAnswer) if (s.IndexOf(m, StringComparison.OrdinalIgnoreCase) >= 0) return true;
        return false;
    }

    // 5) Tool: read the bridge's self-probe result, written independently by another component
    // (bridge/copilot_bridge.py + tools/tool_probe.py) roughly every ~10 min. This file only READS
    // it -- never writes .fleet/tool_probe.json. Shape: {"ts":<epoch float>,"ok":<bool>,
    // "kind":"answer"|"consent_card"|"canned_fallback"|"timeout"|"agent_unreachable"|"error",
    // "detail":<str>}.
    //   GRAY  -- file has never existed (probe disabled / MCP_TOOL_PROBE_SEC=0 on this machine).
    //            Deliberately NOT red: a new/unconfigured feature must never read as an outage.
    //   RED   -- file missing after having looked (can't happen here since we check Exists first,
    //            kept as a safety fallback) OR stale (older than the configured probe interval + 10
    //            min, at least 20 -- ToolProbeStaleAfterMin; the probe itself isn't running) OR the
    //            probe failed with nothing coming back at all.
    //   GREEN -- ok==true AND fresh. ok may now come from a REAL tool call instead of a probe
    //            (record kind "answer" with "evidence":"real_call", at that call's own time).
    //   GRAY  -- also when the bridge reports the probe switched off (tool_probe_idle_min=0).
    //   YELLOW-- consent_card/canned_fallback, or the probe failed while "alive" says a reply
    //            DID arrive. Red is reserved for silence: a failed probe on a chat that is
    //            answering normally used to paint this dot red, and a red dot is read as
    //            "everything is broken" by every user who sees it.
    void PollToolProbeOnce(DateTime now)
    {
        string path = Path.Combine(RepoRoot(), ".fleet", "tool_probe.json");
        if (!File.Exists(path))
        {
            // GRAY ONLY WHEN THE PROBE IS ACTUALLY SWITCHED OFF. "probe disabled", "the bridge
            // never started" and "the probe writer is broken" all produced the same grey dot,
            // and grey means "no evidence expected" -- so two of those three were reported as
            // nothing to see. The setting is readable, so the three can be told apart.
            string probeSec = EnvValue("MCP_TOOL_PROBE_SEC");
            bool disabled = probeSec == "0" || ToolProbeReportedOff();
            SetDot(5, disabled ? HealthState.Gray : HealthState.Yellow,
                   T(disabled ? "hs_tool_detail_none" : "hs_tool_detail_never"), now);
            return;
        }
        try
        {
            var o = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
            double ts = (o != null && o.ContainsKey("ts")) ? Convert.ToDouble(o["ts"]) : 0.0;
            bool ok = (o != null && o.ContainsKey("ok")) && Convert.ToBoolean(o["ok"]);
            string kind = (o != null && o.ContainsKey("kind")) ? Convert.ToString(o["kind"]) : "";
            double ageMin = ts > 0 ? (NowUnix() - ts) / 60.0 : double.MaxValue;
            string ageTxt = ts > 0 ? AgeMinutesText(ageMin) : T("hs_never");
            // A PROBE IN FLIGHT NO LONGER REPLACES THE VERDICT. tools/tool_probe.py used to
            // write kind="checking" over the whole record, so for the 30-180s of a real round
            // trip there was no measured result at all and this dot changed colour every
            // cycle, on a schedule, with nothing wrong. A dot that does that is one people
            // stop reading -- it was reported as a fault, correctly. The transitional state is
            // now a flag beside the last verdict, and the colour keeps reporting what was last
            // actually measured.
            double probingAgeMin = double.MaxValue;
            if (o != null && o.ContainsKey("probing_since"))
            {
                double ps = Convert.ToDouble(o["probing_since"]);
                if (ps > 0) probingAgeMin = (NowUnix() - ps) / 60.0;
            }
            // Only while it is plausibly still running. A probe that started an hour ago and
            // never recorded a result is not "in progress", it is a prober that died.
            bool probing = probingAgeMin < 20.0;

            // TWO TOOL PATHS, AND THIS DOT ONLY EVER WATCHED ONE. Everything above comes from
            // .fleet/tool_probe.json, written solely by the BRIDGE's idle self-probe on its
            // own Edge profile (CDP :9223). Fleet workers call tools over a different
            // transport entirely, and on 2026-09-16 this dot was red -- truthfully, the bridge
            // agent had lost its tool list -- while fleet workers completed 84 tool calls in
            // an hour and a goal finished DONE with the refuter upholding it. A dot labelled
            // "Tool" showed red and tool access was fine.
            //
            // fleet_tool_ok comes from /health (tools/fleet_tool_health.py), derived from the
            // ledger of real calls rather than from a probe. "" or "null" means no calls
            // lately, which is NOT evidence of anything and must not colour the dot.
            // Read through the age gate, not from the field directly: HealthField's contract is
        // that "" means "no evidence", and a stale body has exactly as much evidence in it.
        string fleetTool = (_lastHealthBodyAt > 0
                            && NowUnix() - _lastHealthBodyAt <= HEALTH_BODY_MAX_AGE_S)
                         ? HealthField(_lastHealthBody, "fleet_tool_ok") : "";
            bool fleetWorking = fleetTool == "true";
            bool fleetFailing = fleetTool == "false";

            if (fleetWorking)
            {
                // CURRENT OPERABILITY WINS THE COLOUR. The Fleet is the path this cockpit is
                // supervising. If real Fleet calls are succeeding, Tool is green. A degraded
                // bridge/chat probe remains visible in the detail, but must not turn the Fleet
                // traffic light amber while the work path is demonstrably healthy.
                string detail = ok
                    ? (ageTxt + " " + T("hs_tool_detail_ok"))
                    : (ageTxt + " " + T("hs_tool_detail_bridge_only_down"));
                if (ok && probing) detail += " / " + T("hs_tool_detail_checking");
                SetDot(5, HealthState.Green, detail, now);
            }
            else if (ok && fleetFailing)
                // The bridge can call tools and the fleet cannot. Green here would hide the
                // failure of the path that does the work.
                SetDot(5, HealthState.Yellow, ageTxt + " " + T("hs_tool_detail_fleet_down"), now);
            else if (ToolProbeReportedOff())
                // SWITCHED OFF IN THE SETTINGS (or by the environment): nothing is measured, so
                // nothing is claimed. Not green (no event behind it) and not red (nothing broke):
                // an old record from before it was switched off must not read as a stale outage.
                SetDot(5, HealthState.Gray, T("hs_tool_detail_none"), now);
            else if (ageMin >= ToolProbeStaleAfterMin())
                // How long a green check stays valid FOLLOWS THE CONFIGURED INTERVAL (interval +
                // 10 min, never under the 20 min it always was): with the probe idle-only at 30
                // min a fixed 20 would paint a healthy, idle machine red between probes.
                SetDot(5, HealthState.Red, ageTxt + " " + T("hs_tool_detail_stale"), now);
            else if (kind == "checking" || kind == "starting")
                // A record written by an older prober, which still overwrote the verdict.
                SetDot(5, HealthState.Checking, ageTxt + " " + T("hs_tool_detail_checking"), now);
            else if (ok && probing)
                SetDot(5, HealthState.Green,
                       ageTxt + " " + T("hs_tool_detail_ok") + " / " + T("hs_tool_detail_checking"),
                       now);
            else if (ok)
                SetDot(5, HealthState.Green, ageTxt + " " + T("hs_tool_detail_ok"), now);
            else if (kind == "consent_card" || kind == "canned_fallback")
                SetDot(5, HealthState.Yellow, ageTxt + " " + T("hs_tool_detail_consent"), now);
            else if (o != null && o.ContainsKey("alive") && Convert.ToBoolean(o["alive"]))
                // The chat answered, it just did not complete the probe's round trip. Calling
                // that "no response, reconnect needed" in red told everyone the stack was down
                // while it was serving turns perfectly well -- the one reading a user acts on.
                SetDot(5, HealthState.Yellow, ageTxt + " " + T("hs_tool_detail_no_tool"), now);
            else   // timeout | agent_unreachable | genuinely silent | unrecognized kind
                SetDot(5, HealthState.Red, ageTxt + " " + T("hs_tool_detail_down"), now);
        }
        catch (Exception)
        {
            // Malformed/partial JSON (e.g. read mid-write by the bridge) -- treat as down, not gray,
            // since the file DOES exist (the feature is active, just unreadable right now).
            SetDot(5, HealthState.Red, T("hs_tool_detail_down"), now);
        }
    }

    // The bridge's own account of its probe cadence, read on the poll thread (a fresh small parse;
    // the settings-popup copy is cached separately). Null = no report / old bridge / unreadable.
    Dictionary<string, object> ToolProbeStateForHealth()
    {
        try
        {
            string path = Path.Combine(RepoRoot(), ".fleet", "tool_probe_state.json");
            if (!File.Exists(path)) return null;
            return _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
        }
        catch (Exception) { return null; }
    }
    // True when the bridge says the probe is off (setting 0 or MCP_TOOL_PROBE_SEC<=0).
    bool ToolProbeReportedOff()
    {
        var st = ToolProbeStateForHealth();
        return st != null && st.ContainsKey("enabled") && st["enabled"] != null
               && !Convert.ToBoolean(st["enabled"]);
    }
    double ToolProbeStaleAfterMin()
    {
        var st = ToolProbeStateForHealth();
        return ToolProbeView.StaleAfterMin(st != null ? Dbl(st, "interval_min") : 0.0);
    }

    // "N分前" / "N min ago" -- small formatter local to the Tool dot's tooltip; not routed through
    // T() because it embeds a number (T() keys are static lookups, no interpolation).
    string AgeMinutesText(double ageMin)
    {
        int m = (int)Math.Max(0, Math.Round(ageMin));
        return (_lang == 0) ? (m + "分前") : (m + " min ago");
    }

    // Read the newest .fleet\transcripts\*.jsonl and return the text of its last assistant turn,
    // or null if none. Fully guarded, cheap (reads one file, scans lines). Runs on the poll thread.
    string NewestAssistantText()
    {
        try
        {
            string dir = Path.GetFullPath(Path.Combine(
                AppDomain.CurrentDomain.BaseDirectory, "..", ".fleet", "transcripts"));
            if (!Directory.Exists(dir)) return null;
            string newest = null; DateTime best = DateTime.MinValue;
            foreach (string f in Directory.GetFiles(dir, "*.jsonl"))
            {
                DateTime wt = File.GetLastWriteTimeUtc(f);
                if (wt > best) { best = wt; newest = f; }
            }
            if (newest == null) return null;
            string last = null;
            foreach (string line in File.ReadLines(newest))
            {
                if (line.IndexOf("\"role\"", StringComparison.Ordinal) < 0) continue;
                if (line.IndexOf("\"assistant\"", StringComparison.Ordinal) < 0) continue;
                last = line;   // keep the last assistant line
            }
            if (last == null) return null;
            // extract the "text" field value (simple, tolerant): find "text":" ... unescaped close
            int ti = last.IndexOf("\"text\"", StringComparison.Ordinal);
            if (ti < 0) return "";
            int c = last.IndexOf(':', ti); if (c < 0) return "";
            int q = last.IndexOf('"', c + 1); if (q < 0) return "";
            var sb = new StringBuilder();
            for (int i = q + 1; i < last.Length; i++)
            {
                char ch = last[i];
                if (ch == '\\' && i + 1 < last.Length) { i++; char n = last[i]; sb.Append(n == 'n' ? '\n' : n); continue; }
                if (ch == '"') break;
                sb.Append(ch);
            }
            return sb.ToString();
        }
        catch (Exception) { return null; }
    }

    void SetDot(int i, HealthState s, string detail, DateTime whenUtc)
    {
        lock (_healthLock) { _health[i].State = s; _health[i].Detail = detail; _health[i].Checked = whenUtc; }
    }

    // Extract the T_.../P_... agent id from the configured agent URL in .env. The URL may be
    // '.../chat/?titleId=T_xxx' (deep link) OR '.../chat/agent/T_xxx' — both carry the same id.
    // Matched later (case-insensitively) inside any tab url, including /chat/agent/<id> conversation
    // forms. Returns "" if no agent URL is configured.
    string ExtractAgentMarker()
    {
        string url = EnvValue("MCP_FLEET_AGENT_URL");
        if (string.IsNullOrEmpty(url)) url = EnvValue("MCP_IMPL_AGENT_URL");
        return AgentIdFromUrl(url);
    }
    static string AgentIdFromUrl(string url)
    {
        if (string.IsNullOrEmpty(url) || url == null) return "";
        // titleId=<id>
        int ti = url.IndexOf("titleId=", StringComparison.OrdinalIgnoreCase);
        if (ti >= 0)
        {
            string tail = url.Substring(ti + 8);
            return TrimId(tail);
        }
        // /agent/<id>
        int ai = url.IndexOf("/agent/", StringComparison.OrdinalIgnoreCase);
        if (ai >= 0)
        {
            string tail = url.Substring(ai + 7);
            return TrimId(tail);
        }
        return "";
    }
    // Cut an id token at the first url separator (&, ?, /, #, whitespace). Keeps '.', '-', '_'
    // (agent ids like 'P_552e6eda-...-....dr_work' and 'T_02140b8c-f551-...' contain those).
    static string TrimId(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder();
        foreach (char c in s)
        {
            if (c == '&' || c == '?' || c == '/' || c == '#' || char.IsWhiteSpace(c)) break;
            sb.Append(c);
        }
        return sb.ToString();
    }
}
