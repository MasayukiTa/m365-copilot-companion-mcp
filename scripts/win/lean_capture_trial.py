"""Does blocking the pixels on the capture page change what is captured, or only what it costs?

THE DECISION THIS EXISTS TO MAKE. The capture page is the socket route's last browser cost --
one page, once per token lifetime, on which a real turn runs. Blocking Image, Font, Media and
Stylesheet on it is cheap to write and easy to be wrong about, so it ships behind a flag and
stays off until this says otherwise.

WHAT IT REFUSES TO ACCEPT AS EVIDENCE

  * "the page did not render". Never claimed. The DOM is still built, the scripts still run,
    layout and paint still happen. Only measured RSS and measured lifetime count.
  * "the gpt_id and the variants matched". Too weak. `variants` alone carried 68 feature flags,
    and a template is not two fields -- the WHOLE normalized template is compared, so a field
    nobody thought to look at cannot drift silently.
  * "it worked once". A resource exhaustion that only shows up intermittently needs
    repetition, and a difference that only shows up on a cold browser needs a cold browser.

PAIRED, NOT BATCHED. Arms alternate one at a time. A tenant's page changes through the day, a
browser's RSS drifts as it is used, and running twenty lean captures followed by twenty
ordinary ones measures the afternoon as much as it measures the change.

    python scripts/win/lean_capture_trial.py --iterations 6
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

DEFAULT_CDP = os.environ.get("MCP_FLEET_CDP", "http://127.0.0.1:9222")

_PS_PROCESSES = (
    "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
    "Select-Object CommandLine, WorkingSetSize | ConvertTo-Json -Compress -Depth 3"
)


def edge_rss_mb(profile="copilot-companion-edge"):
    """Total working set of the managed browser, in MB. The number that decides this."""
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_PROCESSES],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return None
    try:
        data = json.loads(raw) if (raw or "").strip() else []
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    total = sum((p.get("WorkingSetSize") or 0) for p in data
                if profile in (p.get("CommandLine") or ""))
    return round(total / 1048576.0, 1)


def _targets(cdp_url):
    import urllib.request
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/list", timeout=5) as fh:
            return [t for t in json.load(fh) if t.get("type") == "page"]
    except Exception:
        return []


def page_count(cdp_url=DEFAULT_CDP):
    return len(_targets(cdp_url))


def copilot_pages(cdp_url=DEFAULT_CDP):
    return [t.get("url", "") for t in _targets(cdp_url)
            if "m365.cloud.microsoft" in (t.get("url") or "")]


def normalized(template):
    """The WHOLE template, with the parts that name one turn removed, as a comparable string.

    Not gpt_id and variants -- everything. A template that differs in a field nobody listed is
    exactly the failure this is looking for, and a comparison built from a list of interesting
    fields can only find differences somebody already anticipated.
    """
    from relay.chathub import RequestTemplate

    volatile = set(RequestTemplate.VOLATILE_QUERY) | {
        "conversationId", "requestId", "messageId", "traceId", "clientRequestId",
        "timestamp", "chatSessionId", "sessionId", "X-ClientRequestId", "text",
    }

    # SCRUBBED BY SHAPE, NOT ONLY BY NAME, and the first trial is why. Two ORDINARY captures
    # produced two "different" templates, and the whole difference was clientCorrelationId
    # and clientInfo.clientSessionId -- per-turn identifiers nobody had put on the list. The
    # check then reported that blocking resources changed the request, which was false and
    # was the harness's own fault. Naming every id a client might mint is the same losing
    # game as naming every character that may precede a marker: a value SHAPED like a fresh
    # identifier is one, whatever its field happens to be called.
    ident = re.compile(r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                       r"|[0-9a-f]{32})$", re.IGNORECASE)

    def scrub(obj):
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in sorted(obj.items()) if k not in volatile}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        if isinstance(obj, str):
            if ident.match(obj):
                return "<identifier>"
            if len(obj) > 200:
                # A LONG FIELD IS NOT AN OPAQUE ONE, and `variants` is the reason this
                # exists. It is the flag list that selects the responding model -- 68 of them
                # on 2026-08-20 -- and collapsing it to "<2766 chars>" made two captures
                # differ on a LENGTH while saying nothing about which flags. Comma-separated
                # lists are compared as sets, because order is not the request.
                if "," in obj:
                    return sorted({p.strip() for p in obj.split(",") if p.strip()})
                return "<%d chars>" % len(obj)
        return obj

    return json.dumps({"query": scrub(template.query), "frame": scrub(template.frame)},
                      ensure_ascii=False, sort_keys=True)


# ---- the grounding probe -------------------------------------------------------------------
#
# THE FAILURE THIS EXISTS TO CATCH. A template that names no agent, or names one the backend
# does not honour, reaches the DEFAULT Copilot -- no connectors, no tenant grounding, none of
# our tools -- and it answers. Fluently. The route would report success, the goal would reach
# DONE, and the only evidence anything was wrong would be an answer built without the data it
# needed. `gpt_id` being non-empty does not prove the backend agreed to it.
#
# So the probe asks something the default Copilot CANNOT answer and looks at whether the reply
# is a refusal of access. It is run per template variant, because two surfaces can capture two
# different templates and only one of them may be honoured.
#
# NOTHING FROM THE ANSWER IS RECORDED. Not the text, not an excerpt. A tenant's mail and
# calendar are what makes this probe work, and a trial's output file is not a place for them:
# what is written down is whether a completion arrived, whether it read as a refusal, the
# length, and a hash so two runs can be compared without either being readable.

PROBE = os.environ.get(
    "MCP_LEAN_PROBE",
    "受信トレイの直近のメールを1件、件名だけ挙げてください。無い場合は「なし」とだけ答えてください。")

#: A default Copilot with no tenant grounding says some version of this. Both languages,
#: because the same backend answers in whichever the prompt used.
NO_ACCESS = (
    "アクセスできません", "アクセスする権限", "確認できません", "取得できません",
    "できませんでした", "I don't have access", "I do not have access",
    "I'm not able to access", "cannot access", "unable to access",
    "I don't have the ability",
)


def grounding_probe(token, template):
    """Ask the captured route something only a grounded agent can answer.

    Returns a dict of PROPERTIES, never content.
    """
    import hashlib

    from relay.chathub import Conversation
    from relay.socket_route import websocket_connect

    out = {"asked": True, "completed": False, "refused_access": None,
           "chars": 0, "sha": "", "error": ""}
    try:
        conv = Conversation(lambda: token, template=template, turn_timeout_s=120.0)
        answer = conv.ask(PROBE, connect=websocket_connect) or ""
        out["completed"] = True
        out["chars"] = len(answer)
        out["sha"] = hashlib.sha256(answer.encode("utf-8")).hexdigest()[:12]
        low = answer.lower()
        out["refused_access"] = any(m.lower() in low for m in NO_ACCESS)
        out["result"] = str(conv.last_result or "")[:80]
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    return out


class _PeakSampler(threading.Thread):
    """Browser RSS sampled THROUGHOUT the capture, not before and after it.

    THE DELTA ACROSS A CAPTURE CANNOT DECIDE THIS, and two runs proved it: three pairs gave
    +27.3 MB for an ordinary capture against +3.1 for a lean one, and the next three gave -1.6
    against +2.7. A working set read once at the start and once at the end is a difference of
    two numbers each of which moved for reasons that have nothing to do with the page -- a
    garbage collection between them can make a capture look free, or free up more than the
    page ever cost.

    What the page costs is how far the browser rises WHILE it is open. That is a peak, and a
    peak has to be sampled.
    """

    def __init__(self, interval=1.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []
        # NOT self._stop: threading.Thread already has one, and shadowing it with an
        # Event made join() call an Event and raise "object is not callable".
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            mb = edge_rss_mb()
            if mb is not None:
                self.samples.append(mb)
            self._halt.wait(self.interval)

    def stop(self):
        self._halt.set()
        self.join(timeout=10)
        return self.samples


def _token_life_min(token):
    """Minutes of life left on a captured token, or None if it cannot be read."""
    try:
        from relay.profile_token import token_life_s
        return token_life_s(token) / 60.0
    except Exception:
        return 0.0


def arm_fn(arm):
    """The capture each arm runs. NAMED HERE so the trial and the fleet cannot drift:
    every arm is a real capture_fn, called exactly as the route calls it."""
    from relay.lean_capture import capture_via_lean_tab
    from relay.profile_token import capture_via_profile
    from relay.socket_route import capture_via_tab

    return {"full": capture_via_tab,
            "lean": capture_via_lean_tab,
            "light": capture_via_profile}[arm]


def one_capture(context, agent_url, arm, cdp_url=DEFAULT_CDP, probe=False):
    """Run one capture on one arm. Returns a record; never raises."""
    fn = arm_fn(arm)
    baseline = edge_rss_mb()
    rec = {"arm": arm, "started": time.strftime("%H:%M:%S"),
           "rss_before": baseline, "pages_before": page_count(cdp_url)}
    sampler = _PeakSampler()
    sampler.start()
    t0 = time.time()
    try:
        token, template = fn(context, agent_url)
        rec.update({"ok": True, "seconds": round(time.time() - t0, 1),
                    "gpt_id": template.gpt_id,
                    "variants": len(template.frame.get("variants") or []),
                    "template": normalized(template),
                    "token_len": len(token),
                    # HOW LONG THE TOKEN LASTS is half the cost. A cheap capture that
                    # returns a token with ten minutes on it is not cheaper than an
                    # expensive one that returns fifty: the route refreshes on a margin,
                    # so a short token means more captures, and the comparison has to be
                    # cost per MINUTE OF ROUTE, not cost per capture.
                    "token_life_min": round(_token_life_min(token))})
        if probe:
            # NOT RUN HERE. websocket_connect drives asyncio, and inside
            # sync_playwright's context that is a second event loop on a thread
            # already running one: the first trial recorded "Cannot run the event
            # loop while another loop is running" for every probe, so all four came
            # back inconclusive and nobody would have known which arm reached the
            # tenant. The material is kept and the question asked afterwards.
            rec["_probe_material"] = (token, template)
    except Exception as exc:
        rec.update({"ok": False, "seconds": round(time.time() - t0, 1),
                    "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
    samples = sampler.stop()
    # HOW FAR THE BROWSER ROSE WHILE THE PAGE WAS OPEN, above where it started. This is what
    # the page costs; see _PeakSampler for why the before/after difference cannot say.
    if samples and baseline is not None:
        rec["rss_peak"] = max(samples)
        rec["rss_rise"] = round(max(samples) - baseline, 1)
        rec["rss_samples"] = len(samples)
    # Settle before reading RSS: the page has just closed and its renderer exits behind it.
    time.sleep(3)
    rec["rss_after"] = edge_rss_mb()
    rec["pages_after"] = page_count(cdp_url)
    rec["copilot_pages_left"] = len(copilot_pages(cdp_url))
    return rec


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


def summarise(records):
    out = {}
    for arm in sorted({r["arm"] for r in records}):
        rows = [r for r in records if r["arm"] == arm]
        ok = [r for r in rows if r.get("ok")]
        secs = sorted(r["seconds"] for r in ok)
        deltas = [r["rss_after"] - r["rss_before"] for r in ok
                  if r.get("rss_after") is not None and r.get("rss_before") is not None]
        rises = [r["rss_rise"] for r in ok if r.get("rss_rise") is not None]
        lives = [r["token_life_min"] for r in ok if r.get("token_life_min") is not None]
        out[arm] = {
            "attempts": len(rows), "succeeded": len(ok),
            "seconds_median": _median(secs), "seconds_max": secs[-1] if secs else None,
            "rss_delta_median": _median(deltas),
            "rss_rise_median": _median(rises),
            "rss_rises": sorted(rises),
            "token_life_median": _median(lives),
            "token_lives": sorted(lives),
            "residue": sum(r.get("copilot_pages_left") or 0 for r in rows),
            "pages_leaked": sum(1 for r in rows
                                if r.get("pages_after") is not None
                                and r.get("pages_before") is not None
                                and r["pages_after"] > r["pages_before"]),
            "templates": sorted({r["template"] for r in ok}),
        }
    return out


def _flatten(obj, prefix=""):
    """Every leaf of a normalized template, as {dotted.path: comparable value}."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, prefix + "." + str(k))
    elif isinstance(obj, list):
        yield prefix, json.dumps(obj, ensure_ascii=False, sort_keys=True)
    else:
        yield prefix, obj


def _field_ranges(full_templates, lean_templates):
    """(fields that vary among ordinary captures, fields where lean shows something new).

    Two separate questions that the old set comparison ran together. A field varying between
    two ORDINARY captures says something about the capture; a field where the lean arm shows a
    value no ordinary capture produced is the only thing that says something about blocking.
    """
    def rows(templates):
        out = {}
        for text in templates:
            for field, value in _flatten(json.loads(text)):
                out.setdefault(field, set()).add(value if isinstance(value, str) else repr(value))
        return out

    fulls, leans = rows(full_templates), rows(lean_templates)
    varying = {f: sorted(v) for f, v in fulls.items() if len(v) > 1}
    novel = {}
    for field, values in leans.items():
        unseen = values - fulls.get(field, set())
        if unseen:
            novel[field] = sorted(unseen)
    return varying, novel


def _explain(field, lean_values, full_templates):
    """What actually differs about `field`, as lines a reader can act on.

    For a list-valued field it is the membership difference and nothing else; for anything
    else, the values themselves. Printing a truncated whole value said "these two 79-item
    lists differ" and left the reader to guess where.
    """
    seen = set()
    for text in full_templates:
        for name, value in _flatten(json.loads(text)):
            if name == field:
                seen.add(value if isinstance(value, str) else repr(value))
    out = []
    for value in lean_values:
        try:
            lean_list, full_lists = set(json.loads(value)), [set(json.loads(v)) for v in seen]
        except Exception:
            out.append("lean: %s   (ordinary: %s)"
                       % (str(value)[:70], " | ".join(str(v)[:40] for v in sorted(seen))))
            continue
        every = set.intersection(*full_lists) if full_lists else set()
        any_ = set.union(*full_lists) if full_lists else set()
        added, missing = sorted(lean_list - any_), sorted(every - lean_list)
        out.append("%d entries; ordinary captures had %d-%d"
                   % (len(lean_list), min(len(s) for s in full_lists) if full_lists else 0,
                      max(len(s) for s in full_lists) if full_lists else 0))
        if added:
            out.append("only in a lean capture: %s" % ", ".join(added))
        if missing:
            out.append("MISSING from a lean capture: %s" % ", ".join(missing))
        if not added and not missing:
            out.append("same membership; the difference is ordering only")
    return out


def report(summary):
    """Print the verdict. Returns the exit code."""
    print()
    # RISE, not delta. The delta across a capture is a difference of two numbers each of which
    # moved for its own reasons: two runs of three pairs gave +27.3 MB against +3.1, then -1.6
    # against +2.7. The rise is how far the browser went above where it started WHILE the page
    # was open, sampled every second, and the individual rises are printed because a median of
    # three hides everything worth knowing.
    print("%-6s %-9s %-10s %-11s %-22s %-9s %s"
          % ("arm", "ok/att", "median s", "rss rise", "token min", "residue", "leaked"))
    for arm in sorted(a for a in summary if not a.startswith("_")):
        s = summary[arm]
        print("%-6s %-9s %-10s %-11s %-22s %-9s %s"
              % (arm, "%d/%d" % (s["succeeded"], s["attempts"]), s["seconds_median"],
                 s["rss_rise_median"], s["token_lives"], s["residue"], s["pages_leaked"]))

    # FULL IS THE BASELINE, and every other arm is judged against it. There is no
    # symmetric comparison to make: "full" is the capture that has been carrying the fleet
    # for weeks, so the question about any cheaper arm is whether it produces something the
    # baseline never produced -- not whether the two agree with each other.
    baseline = summary.get("full") or {}
    full_t = baseline.get("templates") or []
    others = [a for a in sorted(summary) if not a.startswith("_") and a != "full"]
    print()
    print("distinct normalized templates: %s"
          % {a: len(summary[a]["templates"]) for a in ["full"] + others if a in summary})
    if not full_t:
        print("!! the baseline arm produced no successful capture; nothing is comparable.")
        return 1

    bad = 0
    for arm in others:
        s_arm = summary[arm]
        arm_t = s_arm.get("templates") or []
        if not arm_t:
            print("!! %s produced no successful capture." % arm)
            bad += 1
            continue
        # SET EQUALITY IS THE WRONG QUESTION, and asking it produced two false verdicts
        # before anybody checked what actually differed. An ordinary capture is not
        # deterministic: connectedFederatedConnections came back as ["dummyId"] on some and
        # as a real connector on others, because the page had not finished attaching them.
        # Requiring identical SETS makes any such variation read as "the cheaper arm changed
        # the request", which is a claim built from evidence that has nothing to do with it.
        varying, novel = _field_ranges(full_t, arm_t)
        if varying:
            print("fields that vary between BASELINE captures (not attributable to %s):" % arm)
            for field, values in sorted(varying.items()):
                print("   %s: %s" % (field, " | ".join(str(v)[:40] for v in values)))
        if novel:
            print("!! %s SHOWS VALUES THE BASELINE NEVER DID. A capture describing a" % arm)
            print("   different request means a different product answering. Do not adopt.")
            for field, values in sorted(novel.items()):
                # THE DIFFERENCE, NOT THE VALUE. Printing the whole value truncated made a
                # 79-flag variants list look as though it differed in its FIRST entry --
                # the flag that selects the responding model, present in both arms all
                # along. The real difference was one incidental flag on one capture. A
                # report that misleads about which field moved is worse than no report.
                print("   %s:" % field)
                for detail in _explain(field, values, full_t):
                    print("      %s" % detail)
            bad += 1
        else:
            print("%s: request identical to the baseline." % arm)
        if s_arm.get("residue") or s_arm.get("pages_leaked"):
            print("!! %s left pages behind. Do not adopt." % arm)
            bad += 1

    for arm in sorted(a for a in summary if not a.startswith("_")):
        probes = [r["probe"] for r in summary.get("_records", [])
                  if r["arm"] == arm and r.get("probe")]
        if not probes:
            continue
        # A PROBE THAT COULD NOT RUN IS NOT A PROBE THAT FAILED. The first version counted
        # any incomplete probe against the arm, and duly reported that the BASELINE -- the
        # capture that has been carrying the fleet for weeks -- reached something that could
        # not see the tenant, on the strength of one "could not open the socket: TimeoutError".
        # That is a transport fault. It says nothing about grounding, and treating "could not
        # measure" as "measured bad" is how a check earns the right to be ignored.
        refused = sum(1 for pr in probes if pr.get("refused_access"))
        done = sum(1 for pr in probes if pr.get("completed"))
        errored = [pr.get("error") for pr in probes if not pr.get("completed")]
        print("grounding probe %-6s answered %d/%d, refused access %d%s"
              % (arm, done, len(probes), refused,
                 ("  (%d could not run: %s)" % (len(errored), str(errored[0])[:60]))
                 if errored else ""))
        if refused:
            print("!! %s reached something that cannot see the tenant. Do not adopt." % arm)
            bad += 1
        elif done == 0:
            print("!! no probe on %s ever answered; grounding is unmeasured." % arm)
            bad += 1
    return 1 if bad else 0


def _agent_url_from_env_file():
    """The agent URL the fleet itself uses, from the shared .env.

    The trial has to talk to THE SAME SURFACE the fleet does, or it measures a capture of
    something else. Asking the operator to paste a URL invites exactly that mismatch.
    """
    candidates = [os.path.join(os.environ.get("APPDATA", ""), "copilot-bridge", ".env"),
                  os.path.join(REPO, ".env")]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            for line in open(path, encoding="utf-8-sig", errors="replace"):
                if line.startswith("MCP_FLEET_AGENT_URL="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iterations", type=int, default=6, help="captures PER ARM")
    ap.add_argument("--arms", default="full,light",
                    help="comma-separated: full (a turn), light (no turn), lean")
    ap.add_argument("--cdp", default=DEFAULT_CDP)
    ap.add_argument("--agent-url", default=os.environ.get("MCP_FLEET_AGENT_URL", ""))
    ap.add_argument("--out", default=os.path.join(REPO, ".fleet", "lean_capture_trial.json"))
    ap.add_argument("--probe", action="store_true",
                    help="after each capture, ask the route something only a grounded agent "
                         "can answer. Records properties of the reply, never its text.")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    agent_url = args.agent_url or _agent_url_from_env_file()
    if not agent_url:
        print("!! no agent URL. Pass --agent-url, set MCP_FLEET_AGENT_URL, or configure it")
        print("   in the shared .env the fleet reads.")
        return 2

    from playwright.sync_api import sync_playwright

    records = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp, timeout=30000)
        context = browser.contexts[0]
        arms = [a.strip() for a in args.arms.split(",") if a.strip()]
        for n in range(args.iterations):
            for arm in arms:                               # paired, alternating
                rec = one_capture(context, agent_url, arm, args.cdp, args.probe)
                records.append(rec)
                print("  %-4s #%d  %-4s %5.1fs  rss %s -> %s  pages %s -> %s  residue %s  %s"
                      % (rec["arm"], n + 1, "ok" if rec.get("ok") else "FAIL", rec["seconds"],
                         rec["rss_before"], rec["rss_after"], rec["pages_before"],
                         rec["pages_after"], rec["copilot_pages_left"],
                         rec.get("error", "")[:70]), flush=True)

    # OUTSIDE the playwright block, deliberately -- see the note where the material
    # is kept.
    for rec in records:
        material = rec.pop("_probe_material", None)
        if material is not None:
            rec["probe"] = grounding_probe(*material)
            print("  probe %-4s completed=%s refused=%s chars=%s %s"
                  % (rec["arm"], rec["probe"]["completed"],
                     rec["probe"]["refused_access"], rec["probe"]["chars"],
                     rec["probe"]["error"][:60]), flush=True)

    summary = summarise(records)
    summary["_records"] = records
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "records": records}, fh,
                      ensure_ascii=False, indent=2)
    except OSError:
        pass
    rc = report(summary)
    print("\nWritten to %s" % args.out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
