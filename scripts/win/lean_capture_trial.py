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
import subprocess
import sys
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

    def scrub(obj):
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in sorted(obj.items()) if k not in volatile}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        if isinstance(obj, str) and len(obj) > 200:
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


def one_capture(context, agent_url, lean, cdp_url=DEFAULT_CDP, probe=False):
    """Run one capture on one arm. Returns a record; never raises."""
    from relay.lean_capture import capture_via_lean_tab
    from relay.socket_route import capture_via_tab

    fn = capture_via_lean_tab if lean else capture_via_tab
    rec = {"arm": "lean" if lean else "full", "started": time.strftime("%H:%M:%S"),
           "rss_before": edge_rss_mb(), "pages_before": page_count(cdp_url)}
    t0 = time.time()
    try:
        token, template = fn(context, agent_url)
        rec.update({"ok": True, "seconds": round(time.time() - t0, 1),
                    "gpt_id": template.gpt_id,
                    "variants": len(template.frame.get("variants") or []),
                    "template": normalized(template),
                    "token_len": len(token)})
        if probe:
            rec["probe"] = grounding_probe(token, template)
    except Exception as exc:
        rec.update({"ok": False, "seconds": round(time.time() - t0, 1),
                    "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
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
    for arm in ("full", "lean"):
        rows = [r for r in records if r["arm"] == arm]
        ok = [r for r in rows if r.get("ok")]
        secs = sorted(r["seconds"] for r in ok)
        deltas = [r["rss_after"] - r["rss_before"] for r in ok
                  if r.get("rss_after") is not None and r.get("rss_before") is not None]
        out[arm] = {
            "attempts": len(rows), "succeeded": len(ok),
            "seconds_median": _median(secs), "seconds_max": secs[-1] if secs else None,
            "rss_delta_median": _median(deltas),
            "residue": sum(r.get("copilot_pages_left") or 0 for r in rows),
            "pages_leaked": sum(1 for r in rows
                                if r.get("pages_after") is not None
                                and r.get("pages_before") is not None
                                and r["pages_after"] > r["pages_before"]),
            "templates": sorted({r["template"] for r in ok}),
        }
    return out


def report(summary):
    """Print the verdict. Returns the exit code."""
    print()
    print("%-6s %-9s %-10s %-9s %-11s %-9s %s"
          % ("arm", "ok/att", "median s", "max s", "rss delta", "residue", "leaked"))
    for arm in ("full", "lean"):
        s = summary[arm]
        print("%-6s %-9s %-10s %-9s %-11s %-9s %s"
              % (arm, "%d/%d" % (s["succeeded"], s["attempts"]), s["seconds_median"],
                 s["seconds_max"], s["rss_delta_median"], s["residue"], s["pages_leaked"]))

    full_t, lean_t = summary["full"]["templates"], summary["lean"]["templates"]
    print()
    print("distinct normalized templates: full=%d lean=%d" % (len(full_t), len(lean_t)))
    if not lean_t or not full_t:
        print("!! one arm produced no successful capture; nothing is comparable yet.")
        return 1
    if set(full_t) != set(lean_t):
        print("!! THE TEMPLATES DIFFER. A lean capture describes a different request, which")
        print("   means a different product answering. Do not adopt.")
        for t in list(set(lean_t) - set(full_t))[:1]:
            print("   lean-only: %s" % t[:400])
        return 1
    for arm in ("full", "lean"):
        probes = [r["probe"] for r in summary.get("_records", []) if r["arm"] == arm and r.get("probe")]
        if probes:
            refused = sum(1 for p in probes if p.get("refused_access"))
            done = sum(1 for p in probes if p.get("completed"))
            print("grounding probe %-5s completed %d/%d, refused access %d"
                  % (arm, done, len(probes), refused))
            if refused or done < len(probes):
                print("!! the %s arm reached something that cannot see the tenant. Do not adopt."
                      % arm)
                return 1

    if summary["lean"]["residue"] or summary["lean"]["pages_leaked"]:
        print("!! the lean arm left pages behind. Do not adopt.")
        return 1
    print("   identical across arms.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iterations", type=int, default=6, help="captures PER ARM")
    ap.add_argument("--cdp", default=DEFAULT_CDP)
    ap.add_argument("--agent-url", default=os.environ.get("MCP_FLEET_AGENT_URL", ""))
    ap.add_argument("--out", default=os.path.join(REPO, ".fleet", "lean_capture_trial.json"))
    ap.add_argument("--probe", action="store_true",
                    help="after each capture, ask the route something only a grounded agent "
                         "can answer. Records properties of the reply, never its text.")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if not args.agent_url:
        print("!! no agent URL. Pass --agent-url or set MCP_FLEET_AGENT_URL.")
        return 2

    from playwright.sync_api import sync_playwright

    records = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp, timeout=30000)
        context = browser.contexts[0]
        for n in range(args.iterations):
            for lean in (False, True):                     # paired, alternating
                rec = one_capture(context, args.agent_url, lean, args.cdp, args.probe)
                records.append(rec)
                print("  %-4s #%d  %-4s %5.1fs  rss %s -> %s  pages %s -> %s  residue %s  %s"
                      % (rec["arm"], n + 1, "ok" if rec.get("ok") else "FAIL", rec["seconds"],
                         rec["rss_before"], rec["rss_after"], rec["pages_before"],
                         rec["pages_after"], rec["copilot_pages_left"],
                         rec.get("error", "")[:70]), flush=True)

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
