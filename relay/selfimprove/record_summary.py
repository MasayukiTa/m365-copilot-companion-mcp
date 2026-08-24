"""One-line summaries of ledger records, in both languages, cached beside the ledger.

WHY THIS EXISTS. The dashboard's headline is computed from the record -- the event maps to a
fixed verb, the target to the basenames of `changed` -- and that part needs no model. What it
cannot compute is the `reason`: free prose the agent typed, in whichever language it happened
to be working in. Two consequences the operator hit directly:

  * the reason line was doing the work of a title, so a record's headline was its own input
  * toggling the interface to English left those lines in Japanese, because they are not
    interface text -- they are the record

A summary CAN be derived, and a translation of one genuinely needs a model: this is language
conversion, not a lookup table dressed up as intelligence. So the model is used here and
nowhere else on this screen.

THE RECORD IS NEVER REWRITTEN. The ledger is append-only and its `reason` is what was actually
typed; a summary that replaced it would put an unverifiable paraphrase where a record used to
be, which is the failure this whole subsystem exists to prevent. Summaries live in a separate
cache keyed by the record's own hash, so:

  * a summary cannot attach to a different record -- the key is the record's identity
  * an edited record simply misses the cache and shows its raw reason
  * deleting this file returns the screen to exactly what it showed before

Failure is always downwards: no cache entry means the raw reason, which is today's display.

CLI:
    python -m relay.selfimprove.record_summary --list
    python -m relay.selfimprove.record_summary --backfill --limit 5
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Derived, not a record. Untracked with the rest of the runtime state.
CACHE_PATH = os.path.join(_REPO, ".fleet", "selfimprove", "record_summaries.json")

#: What a summary may cost the reader. A summary longer than the line it replaces is not one.
#:
#: Raised from 48/90 after reading the first 29 on screen: the model kept slightly over, the
#: hard cut landed mid-word, and the result looked like a broken sentence rather than a
#: shortened one. The line is wide enough for these and the display wraps.
MAX_JA = 70
MAX_EN = 130

LANGS = ("ja", "en")


def load() -> dict:
    try:
        with io.open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, CACHE_PATH)


def key_of(record: dict) -> str:
    """A record's own hash. Nothing else identifies it -- seq numbers repeat across ledgers."""
    return str((record or {}).get("hash") or "")


def summary_for(record: dict, lang: str, cache: dict = None) -> str:
    """The cached summary in `lang`, or "" when there is none. Never raises."""
    try:
        if lang not in LANGS:
            return ""
        k = key_of(record)
        if not k:
            return ""
        entry = (cache if cache is not None else load()).get(k) or {}
        return str(entry.get(lang) or "")
    except Exception:
        return ""


def missing(records, cache: dict = None) -> list:
    """Records with a reason worth summarising and no entry for every language.

    Three kinds are skipped, all for the same reason -- paying a model for a line nobody will
    read:

      * a reason already short enough to read. Replacing a 30-character sentence with a
        30-character summary spends a call to change nothing.
      * baseline_mismatch. Its reason is a constant string, 24 identical copies in the live
        ledger, and the dashboard folds each one into the re-signing that closed it.
      * records written by test runs against temp directories. They are counted in one line,
        never shown individually.
    """
    cache = load() if cache is None else cache
    out = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        k = key_of(r)
        reason = str(r.get("reason") or "").strip()
        if not k or not reason:
            continue
        if len(reason) <= MAX_JA:
            continue
        if str(r.get("event") or "") == "baseline_mismatch":
            continue
        paths = [str(x) for x in (r.get("changed") or {})]
        if paths and all(("Temp" in x) or ("tmp" in x.lower()) for x in paths):
            continue
        entry = cache.get(k) or {}
        if all(str(entry.get(l) or "").strip() for l in LANGS):
            continue
        out.append(r)
    return out


#: Where a sentence may be cut. Japanese has no spaces, so a length cut lands inside a word
#: unless it is steered to punctuation.
_BREAKS = "\u3002\u3001\uff0e\uff0c.,;: "


def _clean(text: str, limit: int) -> str:
    """Collapse whitespace; if it is over, cut at a break and SAY that it was cut.

    The first pass truncated at exactly the limit, which produced lines like
    "...tools/security.py:279 の no-HTTP コンテキスト拒否文が実行不可の操作を指" -- read on
    screen it looks like a sentence that broke, not one that was shortened, and a reader
    cannot tell whether the summary or the record is at fault.
    """
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    head = t[:limit]
    cut = max(head.rfind(ch) for ch in _BREAKS)
    if cut >= limit // 2:
        head = head[:cut + 1]
    return head.rstrip() + "\u2026"


def parse_reply(text: str) -> dict:
    """Pull {"ja": ..., "en": ...} out of a model reply. Returns {} when it cannot.

    Deliberately strict. A reply that did not answer in the requested shape is not massaged
    into one: the alternative to a summary is the raw reason, which is a perfectly good
    outcome, and guessing at malformed output is how a wrong summary gets stored.
    """
    try:
        s = str(text or "").strip()
        if "```" in s:
            parts = s.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                if p.startswith("{"):
                    s = p
                    break
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return {}
        obj = json.loads(s[i:j + 1])
        if not isinstance(obj, dict):
            return {}
        ja = _clean(obj.get("ja"), MAX_JA)
        en = _clean(obj.get("en"), MAX_EN)
        if not ja or not en:
            return {}
        return {"ja": ja, "en": en}
    except Exception:
        return {}


def build_prompt(record: dict) -> str:
    """Extractive by construction: the model is asked to shorten and translate, not to judge.

    It is given the computed facts (event, files) so it does not have to infer them and cannot
    contradict them, and told in as many words that anything not present in the reason must not
    appear in the summary.
    """
    reason = str((record or {}).get("reason") or "").strip()
    event = str((record or {}).get("event") or "")
    files = sorted((record or {}).get("changed") or {})
    return (
        "次の記録を1行で要約してください。\n"
        "- 記録に書かれていないことを足さない。評価や推測を書かない。\n"
        "- 事実の圧縮と翻訳だけを行う。\n"
        "- ja は全角%d字以内、en は%d文字以内。\n"
        "- 出力は JSON 1個だけ: {\"ja\": \"...\", \"en\": \"...\"}\n\n"
        "イベント: %s\n対象ファイル: %s\n記録本文:\n%s\n"
        % (MAX_JA, MAX_EN, event, ", ".join(files) or "(なし)", reason)
    )


def backfill(records, ask, *, limit: int = 0, model: str = "", log=None) -> dict:
    """Fill missing summaries by calling `ask(prompt) -> str`. Returns a small report.

    `ask` is injected for the same reason the socket route injects its connect function: this
    module has no business choosing how to reach a model, and the seam is what lets the whole
    thing be tested without a browser.

    Every generated pair is logged next to the first line of the source reason, because the
    only real check on an extractive summary is a person reading both. At this scale -- 27
    records and a few a week -- reading all of them is a realistic ask.
    """
    # A LOG MUST NOT BE ABLE TO KILL THE WORK IT DESCRIBES. Measured: a model reply carried an
    # em dash, the console was cp932, print raised UnicodeEncodeError, and the whole backfill
    # died -- after generating most of the records and before anything was saved. The run
    # exited 0 because it was behind a pipe, so it looked like a success that had produced
    # nothing.
    _log = log or (lambda m: None)

    def log(m):
        try:
            _log(m)
        except Exception:
            pass

    cache = load()
    todo = missing(records, cache)
    if limit and limit > 0:
        todo = todo[:limit]
    done, failed = 0, 0
    for r in todo:
        k = key_of(r)
        try:
            reply = ask(build_prompt(r))
        except Exception as exc:
            failed += 1
            log("FAILED %s: %s" % (k[:12], exc))
            continue
        pair = parse_reply(reply)
        if not pair:
            failed += 1
            log("UNUSABLE REPLY %s: %r" % (k[:12], str(reply)[:120]))
            continue
        pair["model"] = model
        pair["ts"] = time.time()
        cache[k] = pair
        # SAVED AS IT GOES. Saving only at the end meant one exception anywhere in the loop
        # discarded every summary generated before it -- 28 model calls thrown away by a
        # print. At this scale the write is nothing; losing the run is not.
        try:
            save(cache)
        except Exception:
            pass
        done += 1
        first = " ".join(str(r.get("reason") or "").split())[:110]
        log("%s\n    source : %s\n    ja     : %s\n    en     : %s"
            % (k[:12], first, pair["ja"], pair["en"]))
    return {"generated": done, "failed": failed, "remaining": len(missing(records, cache))}


# ── the ledger, read here so the CLI does not need the dashboard ────────────────────

def ledger_records() -> list:
    try:
        from relay.selfimprove import authority_ledger as AL
        rows = []
        with io.open(AL.DEFAULT_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
        return rows
    except Exception:
        return []


def _copilot_asker(cdp_url: str, timeout_s: float = 180.0, agent_url: str = ""):
    """An `ask` backed by the socket route. Raises if a browser cannot be reached.

    Raising rather than degrading is deliberate: a backfill that quietly produced nothing
    would look exactly like a backfill with nothing to do.
    """
    # The agent URL lives in .env like every other relay setting; a CLI that only read the
    # process environment would fail on a machine where everything else works.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    from playwright.sync_api import sync_playwright

    from relay.socket_route import SocketRoute, capture_via_tab, websocket_connect

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_url)
    if not browser.contexts:
        raise RuntimeError("no browser context at %s" % cdp_url)
    context = browser.contexts[0]

    route = SocketRoute(capture_fn=capture_via_tab, connect_fn=websocket_connect,
                        log=lambda m: print(m, flush=True))
    # AN AGENT SURFACE, NOT THE DEFAULT CHAT. capture refuses a request that names no agent --
    # a socket built from one would reach the default Copilot instead, which is a different
    # assistant answering as if it were this one. The fleet's own agent is the right surface
    # and its URL is already configured; nothing here invents a destination.
    base = (agent_url or os.environ.get("MCP_SUMMARY_AGENT_URL")
            or os.environ.get("MCP_FLEET_AGENT_URL")
            or os.environ.get("MCP_IMPL_AGENT_URL") or "").strip()
    if not base:
        raise RuntimeError("no agent URL configured (MCP_FLEET_AGENT_URL / MCP_IMPL_AGENT_URL)")
    if route.needs_refresh(base) and not route.refresh(context, base):
        raise RuntimeError("could not capture a socket template from the configured agent")
    state = {"driver": route.driver_for("summary", agent_url=base, turn_timeout_s=timeout_s)}
    if state["driver"] is None:
        raise RuntimeError("socket route declined to provide a driver")

    def ask(prompt: str) -> str:
        """One turn, waited out.

        `send` STARTS a turn and returns immediately -- it runs the exchange on a thread, so
        the tab driver and this one present the same non-blocking shape to the fleet's
        round-robin. Reading straight after it returns whatever had arrived by then, which for
        a turn that has not started streaming is the empty string; the first attempt at this
        recorded exactly that, and the next call then hit "a turn is already running".

        A driver that failed stays failed by design (the fleet's answer is to open a tab), so
        a fresh one is taken rather than every later record inheriting the first one's error.
        """
        import time as _t

        driver = state["driver"]
        if driver.failed:
            driver = route.driver_for("summary", agent_url=base, turn_timeout_s=timeout_s)
            if driver is None:
                raise RuntimeError("socket route declined to provide a driver")
            state["driver"] = driver

        before = driver.response_block_count()
        driver.send(prompt)
        deadline = _t.time() + timeout_s
        while _t.time() < deadline:
            if driver.failed:
                raise RuntimeError(driver.failed)
            if driver.response_block_count() > before:
                return driver.read_last_reply_clean()
            _t.sleep(1.0)
        raise RuntimeError("no reply within %ds" % int(timeout_s))

    return ask


def _cli(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show which records lack a summary")
    ap.add_argument("--backfill", action="store_true", help="generate the missing ones")
    ap.add_argument("--limit", type=int, default=0, help="stop after N records")
    ap.add_argument("--cdp", default="http://localhost:9222",
                    help="CDP endpoint of the browser already signed in to Copilot")
    ap.add_argument("--agent-url", default="",
                    help="agent surface to ask; defaults to the configured fleet agent")
    args = ap.parse_args(argv)

    records = ledger_records()
    todo = missing(records)
    print("ledger records : %d" % len(records))
    print("without summary: %d" % len(todo))
    if args.list or not args.backfill:
        for r in todo[:args.limit or 20]:
            print("  %s  %s  %s" % (key_of(r)[:12], r.get("event"),
                                    " ".join(str(r.get("reason") or "").split())[:80]))
        return 0

    def emit(m):
        """The console here is cp932 and a model reply may hold anything. Printable-or-not is
        a property of the terminal, never a reason to lose a record."""
        try:
            sys.stdout.write(str(m) + "\n")
        except Exception:
            enc = getattr(sys.stdout, "encoding", "") or "ascii"
            sys.stdout.write(str(m).encode(enc, "replace").decode(enc, "replace") + "\n")
        try:
            sys.stdout.flush()
        except Exception:
            pass

    ask = _copilot_asker(args.cdp, agent_url=args.agent_url)
    report = backfill(records, ask, limit=args.limit, model="copilot", log=emit)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
