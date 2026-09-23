# -*- coding: utf-8 -*-
"""The chat window's send path, EXECUTED -- and compared with the code it was extracted from.

## Why this exists

Every other test of this path reads `ui/CopilotChat.cs` as text. A text assertion says a
spelling is present; it cannot say which branch a given input takes, whether an effect happens
before or after another, or whether it happens at all. That gap was not hypothetical: on
2026-09-22 `ui/test_a_fleet_conversation_can_be_answered.py` was green on
`g["resume_conv"] = c.ConvUrl` while the hop in the middle dropped the field.

So the send path -- capacity reroute, `!`, the research router, the page-pinning doors, the
fleet steer / follow-up / `/goal` split, the payload -- now lives in `ui/ChatSend.cs`, with its
effects behind `IChatSendEffects`. This file compiles it with the real csc and RUNS it.

## What it checks

* **Differential.** `ui/testdata/ChatDecisionsOriginal.cs` is the pre-extraction code, copied
  from commit f26c4de with the smallest adapters that let it run. Every case is run through
  both, against the same recording world, and the two must agree on everything: the ordered
  effect trace, the state left behind, and three status decisions. The expected answers come
  from RUNNING the code that shipped, not from what anyone thinks it did.
  Several divergences are declared, by rule or by case id: the header chip no longer counts a
  "stuck", "maxturns" or "content_refused" worker as active (see `_EXPECTED_DIVERGENCE`), and a
  handful of specific cases changed which command is sent -- a now-terminal worker is not
  steered, a failed capacity-reroute write is reported instead of silently "queued", and a
  fleet conversation at capacity uses its own path instead of the bare-goal reroute (see
  `_DECLARED_BEHAVIOUR_CHANGES`).
* **Orchestration.** Properties of the extracted flow per case: which door or command, how
  many dispatches, the exact payload, what the person sees when it is refused or fails.
* **Cross-language.** status.json is produced by the fleet's own writer
  (`relay.fleet_runner._snapshot` + `_write_atomic`) wherever that writer can express the case;
  the handful it cannot (an `idle` key -- nothing in relay/ writes one --, malformed text, a
  BOM, odd types) are written by hand in the shape the window reads. A successful command is
  written by the real `FleetCommands.Write` and read back with `fleet_runner.read_commands` /
  `goals_from_command`, and every field must survive exactly.

## What is still NOT tested by execution

This is not "only the event wiring remains". Two things remain:

1. **The WPF key event reaching the send method** -- `_input.PreviewKeyDown` /
   `_send.Click` calling `ChatWindow.DoSend`, and the router bar's buttons calling `SendText`.
2. **The real effects behind the interface** -- the HTTP GETs to the bridge, the stream, what
   `AddAssistant` draws, the banner's button starting the stack, the file read of the real
   `.fleet/status.json`. The fakes here record that each was asked for, in order; they do not
   prove that the window's implementation of each does what its name says.

## Skips

Only on a non-Windows host. On Windows, a missing csc skips unless `REQUIRE_CSC=1`, which CI
sets, and then it fails. A compile failure, a harness failure, or zero cases run always fail.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

UI = os.path.join(REPO, "ui")
FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")

#: Compiled together, in this order, and nothing else. ChatSend.cs is the shipped file;
#: FleetCommands.cs is the shipped writer; the other two are test-only.
SOURCES = [
    os.path.join(UI, "ChatSend.cs"),
    os.path.join(UI, "FleetCommands.cs"),
    os.path.join(UI, "testdata", "ChatDecisionsOriginal.cs"),
    os.path.join(UI, "testdata", "ChatSendHarness.cs"),
]

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="the code under test is C# compiled by the .NET Framework csc, which exists only "
           "on Windows; asserting on its source text instead is what this file replaces")


# ── status.json fixtures ───────────────────────────────────────────────────────────────────

T1 = r"C:\repo\.fleet\transcripts\1726000000_w0.jsonl"
T2 = r"C:\repo\.fleet\transcripts\1726000000_w1.jsonl"
T3 = r"C:\repo\.fleet\transcripts\1726000000_w2.jsonl"
T4 = r"C:\repo\.fleet\transcripts\1726000000_w3.jsonl"
T5 = r"C:\repo\.fleet\transcripts\1726000000_w4.jsonl"

_ALL_STATUSES = ["done", "resolved", "failed", "error", "cancelled", "stopped", "stuck",
                 "pending", "maxturns", "content_refused", "running", "verifying", "Stuck", "Done"]


def _slug(status):
    """A name for `status` that no other status shares IGNORING CASE. "Done" and "done" are one
    name to this filesystem and to the transcript match (OrdinalIgnoreCase) -- the first draft
    used the status itself and the two collided on both, which is not what it meant to test."""
    return status.lower() + ("_upper" if status != status.lower() else "")


def _tr(status):
    return r"C:\repo\.fleet\transcripts\all_%s.jsonl" % _slug(status)


class _Worker(SimpleNamespace):
    """What `fleet_runner._snapshot` reads off a worker, and nothing more."""

    def tab_load(self):
        return self.tabs


def _w(name, status, transcript, tabs=1):
    return _Worker(name=name, goal="goal of " + name, status=status, outcome="", turn=1,
                   max_turns=10, reason="", last_response="", transcript=transcript, tabs=tabs)


#: Written by the fleet's own writer. (name -> (workers, max_concurrent))
REAL = {
    # T1 live; T2 stuck; T3 pending; T4 done. 4 tabs open of 6 -> room.
    "room": ([_w("w0", "running", T1), _w("w1", "stuck", T2), _w("w2", "pending", T3),
              _w("w3", "done", T4)], 6),
    # two live workers, two tabs, max 2 -> at capacity
    "cap": ([_w("w0", "running", T1), _w("w4", "running", T5)], 2),
    # every worker terminal -> the writer says running=False
    "finished": ([_w("w0", "done", T1), _w("w1", "stuck", T2)], 2),
    # the same transcript twice; the FIRST row decides
    "dup_dead_first": ([_w("w0", "done", T1), _w("w7", "running", T1)], 6),
    "dup_live_first": ([_w("w0", "running", T1), _w("w7", "done", T1)], 6),
    # the transcript path differs from the conversation's only in case
    "case_path": ([_w("w0", "running", T1.upper())], 6),
    # one worker in each status the window has to classify (the chip's count and the steer)
    "all_statuses": ([_w("x_" + s, s, _tr(s)) for s in _ALL_STATUSES], 99),
    # an uppercase terminal status: the steer lookup is case-sensitive, the chip is not
    "upper_done": ([_w("w0", "Done", T1)], 6),
    # max_concurrent 0 -> never "at capacity", however many tabs
    "maxc_zero": ([_w("w0", "running", T1), _w("w4", "running", T5)], 0),
}

_CAP_HAND = {"running": True, "open_tabs": 2, "max_concurrent": 2,
             "workers": [{"name": "w0", "status": "running", "transcript": T1}]}

#: Written by hand: shapes the real writer never produces but the file can still hold.
HAND = {
    # nothing in relay/ writes `idle`; the window reads it anyway
    "idle_key": json.dumps({"running": True, "idle": True, "open_tabs": 5, "max_concurrent": 2,
                            "workers": [{"name": "w0", "status": "running", "transcript": T1}]}),
    "malformed": "{ not json",
    "array_root": "[1, 2, 3]",
    "empty_text": "",
    "nulls": json.dumps({"running": True, "open_tabs": None, "max_concurrent": None,
                         "workers": None}),
    "strings": json.dumps({"running": "true", "open_tabs": "3", "max_concurrent": "3",
                           "workers": [{"name": "w0", "status": "running", "transcript": T1}]}),
    "bad_bool": json.dumps({"running": "yes", "open_tabs": 3, "max_concurrent": 3,
                            "workers": [{"name": "w0", "status": "running", "transcript": T1}]}),
    "odd_rows": json.dumps({"running": True, "open_tabs": 1, "max_concurrent": 6,
                            "workers": [1, None, "x", [], {"status": "running"},
                                        {"name": "w9", "status": None, "transcript": T1}]}),
    "fraction": json.dumps({"running": True, "open_tabs": 2.5, "max_concurrent": 2,
                            "workers": []}),
    "no_running": json.dumps({"open_tabs": 9, "max_concurrent": 2,
                              "workers": [{"name": "w0", "status": "running", "transcript": T1}]}),
}

#: Bytes, not text: a BOM in front of an at-capacity status.
BYTES = {"bom_cap": b"\xef\xbb\xbf" + json.dumps(_CAP_HAND).encode("utf-8")}


def _write_status(root, key):
    """A directory holding status.json for `key` (or none, for "none"/"missing")."""
    from relay import fleet_runner as FR

    d = os.path.join(root, "status", key)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "status.json")
    if key in REAL:
        workers, maxc = REAL[key]
        FR._write_atomic(p, FR._snapshot(workers, 1726000000.0, len(workers),
                                         max_concurrent=maxc))
    elif key in HAND:
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(HAND[key])
    elif key in BYTES:
        with open(p, "wb") as fh:
            fh.write(BYTES[key])
    elif key != "none":
        raise KeyError(key)
    return p


#: Statuses IsTerminalWorkerStatus excludes from "active" now and did not before 2026-09-24.
#: "stuck" was ALREADY terminal to the steer lookup in the shipped code (ChatDecisionsOriginal
#: .cs's LiveWorkerFor lists it); only the chip's separate copy (ReadActiveFleetWorkerCount)
#: missed it, so "stuck" only ever moves active_count. "maxturns" and "content_refused" were
#: missing from BOTH copies in the shipped code, so they move active_count too -- but a fleet
#: conversation whose transcript is pinned to a worker in one of these two statuses also changes
#: which COMMAND gets sent (steer vs. follow-up), which active_count cannot express. Those
#: specific cases are excluded from the blanket equality below and asserted by their own test
#: instead (see _DECLARED_BEHAVIOUR_CHANGES and test_a_now_terminal_worker_is_not_steered).
_NEWLY_EXCLUDED_FROM_ACTIVE = ("stuck", "maxturns", "content_refused")


def _excluded_now_count(key):
    """How many workers in fixture `key` the chip used to count as active and no longer does."""
    if key not in REAL:
        return 0
    return sum(1 for w in REAL[key][0] if w.status.lower() in _NEWLY_EXCLUDED_FROM_ACTIVE)


# ── the cases ─────────────────────────────────────────────────────────────────────────────

SESS = "sess:11111111-2222-3333-4444-555555555555"
URL = "https://m365.cloud.microsoft/chat/conversation/abc?x=1&y=2"


def _chat(**kw):
    c = {"id": "c1", "source": "chat", "conv_url": "", "name": "", "messages": []}
    c.update(kw)
    return c


def _fleet(**kw):
    c = {"id": "f1", "source": "fleet", "conv_url": SESS, "name": "w0", "transcript": T1,
         "goal": "  summarise the release notes  ", "title": "summarise the rel…",
         "messages": [["U", "summarise the release notes"], ["A", "done"]]}
    c.update(kw)
    return c


def _case(cid, input_text, conv, status="none", **kw):
    k = {"id": cid, "input": input_text, "conv": conv, "status": status}
    k.update(kw)
    return k


CASES = [
    # ── nothing to send ──
    _case("empty", "", _chat()),
    _case("whitespace", "   \t\n ", _chat()),
    _case("send_disabled", "hello", _chat(), send_enabled=False),
    # ── /help ──
    _case("help", "/help", _chat()),
    _case("help_upper_padded", "  /HELP  ", _chat()),
    # ── doors ──
    _case("door_new", "hello", _chat()),
    _case("door_new_null_url", "hello", _chat(conv_url=None)),
    _case("door_switch", "hello", _chat(conv_url=URL, messages=[["U", "a"]])),
    _case("door_resume_guid", "hello", _chat(conv_url=SESS, messages=[["U", "a"]])),
    _case("door_sess_upper_is_a_url", "hello", _chat(conv_url="SESS:abc", messages=[["U", "a"]])),
    _case("door_sid", "hello", _chat(name="sid-123", messages=[["U", "a"]])),
    _case("door_unknown", "hello", _chat(messages=[["U", "a"], ["A", "b"]])),
    _case("door_unknown_source_empty_with_name", "hello",
          _chat(source="", name="sid-9", messages=[["U", "a"]])),
    _case("door_on_page", "hello", _chat(messages=[["U", "a"]]), page="same"),
    _case("door_page_null", "hello", _chat(conv_url=URL), page="none"),
    _case("door_resume_fails", "hello", _chat(conv_url=SESS), http_fail=["/resume"]),
    _case("door_new_fails", "hello", _chat(), http_fail=["/new"]),
    _case("door_titled_kept", "second line\nmore", _chat(title="kept title")),
    _case("door_long_title", "x" * 60 + "\nsecond", _chat()),
    _case("door_not_listed", "hello", _chat(), listed=False),
    # ── no conversation at all ──
    _case("no_conv", "hello", None),
    # ── bridge down ──
    _case("offline_stays_down", "hello there", _chat(), bridge_reachable=False,
          http_fail=["/conv"]),
    _case("offline_recovers", "hello there", _chat(), bridge_reachable=False),
    # ── capacity ──
    _case("cap_queued", "plain words", _chat(), status="cap"),
    _case("cap_forced", "!  urgent fix", _chat(), status="cap"),
    _case("cap_bang_alone", "!", _chat(), status="cap"),
    _case("cap_bang_space", "!   ", _chat(), status="cap"),
    _case("bang_alone_with_room", "!", _chat(), status="room"),
    _case("cap_slash_not_rerouted", "/goal a new job", _chat(), status="cap"),
    _case("cap_research_slash", "/research compare x", _chat(), status="cap"),
    _case("cap_append_fails_is_reported", "plain words", _chat(), status="cap", append_ok=False),
    _case("cap_bom", "plain words", _chat(), status="bom_cap"),
    _case("cap_strings", "plain words", _chat(), status="strings"),
    _case("cap_idle_key", "plain words", _chat(), status="idle_key"),
    _case("cap_bad_bool", "plain words", _chat(), status="bad_bool"),
    _case("cap_no_running", "plain words", _chat(), status="no_running"),
    _case("cap_maxc_zero", "plain words", _chat(), status="maxc_zero"),
    _case("cap_fraction", "plain words", _chat(), status="fraction"),
    _case("cap_locked", "plain words", _chat(), status="cap", status_locked=True),
    _case("cap_fleet_steer_not_rerouted", "a follow-up", _fleet(), status="cap"),
    _case("cap_fleet_follow_up_not_rerouted", "a follow-up", _fleet(transcript=T2), status="cap"),
    # ── research router ──
    _case("research_en", "please investigate the outage", _chat()),
    _case("research_ja", "これを調査して", _chat()),
    _case("research_router_already_shown", "please investigate the outage", _chat(),
          router_shown=True),
    _case("research_slash", "/research the outage", _chat()),
    _case("router_button_research", None, _chat(), entry="SendText",
          send_text="/research please investigate"),
    # ── fleet conversation: steer ──
    _case("fleet_steer_live", "keep going but shorter", _fleet(), status="room"),
    _case("fleet_steer_append_fails", "keep going", _fleet(), status="room", append_ok=False),
    _case("fleet_steer_idle_key_still_live", "keep going", _fleet(), status="idle_key"),
    _case("fleet_steer_upper_done_is_live", "keep going", _fleet(), status="upper_done"),
    _case("fleet_steer_case_path", "keep going", _fleet(), status="case_path"),
    _case("fleet_dup_live_first", "keep going", _fleet(), status="dup_live_first"),
    _case("fleet_bare_goal_word_steers", "/goal", _fleet(), status="room"),
    # ── fleet conversation: /goal ──
    _case("fleet_goal_while_live", "/goal write the changelog", _fleet(), status="room"),
    _case("fleet_goal_upper", "/GOAL write the changelog", _fleet(), status="room"),
    _case("fleet_goal_empty_via_router", None, _fleet(), status="room", entry="SendText",
          send_text="/goal    "),
    _case("fleet_goal_no_goal_text", "/goal write it", _fleet(goal=""), status="none"),
    # ── fleet conversation: follow-up ──
    _case("fleet_follow_ja_running", "and the dates?", _fleet(transcript=T2), status="room",
          lang=0),
    _case("fleet_follow_en_idle", "and the dates?", _fleet(), status="none", lang=1),
    _case("fleet_follow_pending_is_not_live", "and the dates?", _fleet(transcript=T3),
          status="room"),
    _case("fleet_follow_finished", "and the dates?", _fleet(), status="finished"),
    _case("fleet_dup_dead_first", "and the dates?", _fleet(), status="dup_dead_first"),
    _case("fleet_follow_no_transcript", "and the dates?", _fleet(transcript=""), status="room"),
    _case("fleet_follow_malformed", "and the dates?", _fleet(), status="malformed"),
    _case("fleet_follow_array_root", "and the dates?", _fleet(), status="array_root"),
    _case("fleet_follow_empty_status", "and the dates?", _fleet(), status="empty_text"),
    _case("fleet_follow_nulls", "and the dates?", _fleet(), status="nulls"),
    _case("fleet_follow_odd_rows", "and the dates?", _fleet(), status="odd_rows"),
    _case("fleet_follow_locked", "and the dates?", _fleet(), status="room", status_locked=True),
    _case("fleet_follow_sess_upper", "and the dates?", _fleet(conv_url="SESS:abc-def"),
          status="none"),
    _case("fleet_follow_no_guid", "and the dates?", _fleet(conv_url=""), status="none"),
    _case("fleet_follow_null_url", "and the dates?", _fleet(conv_url=None), status="none"),
    _case("fleet_follow_url_not_sess", "and the dates?", _fleet(conv_url=URL), status="none"),
    _case("fleet_follow_append_fails", "and the dates?", _fleet(), status="none",
          append_ok=False),
    _case("fleet_no_goal_empty", "and the dates?", _fleet(goal=""), status="none"),
    _case("fleet_no_goal_spaces", "and the dates?", _fleet(goal="   "), status="none"),
    _case("fleet_no_goal_null", "and the dates?", _fleet(goal=None), status="none"),
    _case("fleet_on_page_still_fleet", "and the dates?", _fleet(), status="none", page="same"),
    _case("fleet_research_router_first", "please investigate why", _fleet(), status="room"),
] + [
    # the steer lookup, one status at a time (the chip's count rides on every case)
    _case("status_" + _slug(s), "anything", _fleet(transcript=_tr(s)), status="all_statuses")
    for s in _ALL_STATUSES
]


# ── build + run, once ─────────────────────────────────────────────────────────────────────

def _require_csc():
    return os.environ.get("REQUIRE_CSC", "").strip() == "1"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    if not os.path.isfile(CSC):
        msg = "csc.exe is not at %s" % CSC
        if _require_csc():
            pytest.fail(msg + " and REQUIRE_CSC=1")
        pytest.skip(msg)
    d = tmp_path_factory.mktemp("chatsend")
    exe = str(d / "ChatSendHarness.exe")
    r = childproc.run([CSC, "/nologo", "/target:exe", "/out:" + exe,
                       "/r:" + os.path.join(FW, "System.Web.Extensions.dll")] + SOURCES,
                      timeout=300)
    assert r.returncode == 0 and os.path.isfile(exe), (
        "csc could not build the send path with its oracle (rc=%s):\n%s\n%s"
        % (r.returncode, r.stdout, r.stderr))
    return exe, d


@pytest.fixture(scope="module")
def results(harness):
    exe, d = harness
    root = str(d / "run")
    os.makedirs(root)
    cases = []
    for k in CASES:
        k = dict(k)
        k["status_path"] = _write_status(root, k.pop("status"))
        cases.append(k)
    ids = [k["id"] for k in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    cpath = os.path.join(root, "cases.json")
    rpath = os.path.join(root, "results.json")
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(cases, fh, ensure_ascii=False)
    work = os.path.join(root, "commands")
    r = childproc.run([exe, cpath, rpath, work], timeout=180)
    assert r.returncode == 0, "the harness failed (rc=%s):\n%s\n%s" % (
        r.returncode, r.stdout, r.stderr)
    with open(rpath, encoding="utf-8") as fh:
        got = json.load(fh)
    return {"cases": {k["id"]: k for k in cases}, "got": got, "work": work}


def _x(results, cid):
    """The extracted side's outcome for one case, by id."""
    return results["got"][cid]["extracted"]


def _payloads(out, key=None):
    """(key, item) for every AppendCommand in a trace, in order."""
    found = []
    for line in out["trace"]:
        if line.startswith("AppendCommand|"):
            _, k, js = line.split("|", 2)
            if key is None or k == key:
                found.append((k, json.loads(js)))
    return found


def _said(out):
    return [ln[len("AddAssistant|"):] for ln in out["trace"] if ln.startswith("AddAssistant|")]


def _gets(out):
    return [ln for ln in out["trace"] if ln.startswith("HttpGet|")]


# ── the run itself ────────────────────────────────────────────────────────────────────────

def test_every_case_ran_and_is_matched_by_id(results):
    got, cases = results["got"], results["cases"]
    assert len(cases) > 0 and len(got) > 0, "zero cases ran"
    assert set(got) == set(cases), (
        "cases and results disagree -- missing %s, unexpected %s"
        % (sorted(set(cases) - set(got)), sorted(set(got) - set(cases))))
    for cid, r in got.items():
        assert set(r) == {"original", "extracted"}, cid


# ── differential: the extracted code does what the shipped code did ───────────────────────

#: A DELIBERATE DIFFERENCE, stated as a rule rather than a list of cases. The chip's terminal
#: list (ReadActiveFleetWorkerCount) had no "stuck" while the steer lookup's did, and its
#: comment claimed they matched. Both now use ChatSend.IsTerminalWorkerStatus, which as of
#: 2026-09-24 also gained "maxturns" and "content_refused" (relay/relay_fleet.py's own TERMINAL
#: set has all three). So the extracted active count is the original minus every worker in one
#: of those three statuses (any case -- the chip lowercases), and NOTHING ELSE differs -- except
#: the cases in _DECLARED_BEHAVIOUR_CHANGES below, which change more than a count and are
#: excluded from the blanket comparison entirely.
def _EXPECTED_DIVERGENCE(case):
    if case.get("status_locked"):
        return 0            # neither side can read a locked file; both count 0
    return _excluded_now_count(case["status_path"].replace("\\", "/").split("/")[-2])


#: Cases excluded from the blanket equality below because a 2026-09-24 fix changed WHICH
#: COMMAND is sent, not just a count, so the oracle (frozen, pre-fix behaviour) and the
#: extracted code cannot be made to agree by adjusting a field. Each is asserted by its own
#: test instead; the value says which test and why.
_DECLARED_BEHAVIOUR_CHANGES = {
    "status_maxturns":
        "the conversation's transcript now matches a terminal (maxturns) worker; the oracle "
        "still steers it, the extracted code falls through to the follow-up instead -- see "
        "test_a_now_terminal_worker_is_not_steered",
    "status_content_refused":
        "same fix as status_maxturns, for content_refused -- see "
        "test_a_now_terminal_worker_is_not_steered",
    "cap_append_fails_is_reported":
        "DoSend now checks the capacity reroute's AppendCommand result and keeps the typed "
        "text on failure; the oracle never checked it -- see "
        "test_a_failed_capacity_enqueue_is_reported_and_keeps_the_text",
    "cap_fleet_steer_not_rerouted":
        "a fleet conversation at capacity now goes through SendToFleetConversation (here: a "
        "steer) instead of the bare-goal reroute -- see "
        "test_at_capacity_a_fleet_conversation_uses_its_own_path",
    "cap_fleet_follow_up_not_rerouted":
        "same fix as cap_fleet_steer_not_rerouted, for the no-live-worker (follow-up) branch "
        "-- see test_at_capacity_a_fleet_conversation_uses_its_own_path",
}


def test_the_extracted_send_path_does_exactly_what_the_original_did(results):
    diffs = []
    for cid, r in sorted(results["got"].items()):
        if cid in _DECLARED_BEHAVIOUR_CHANGES:
            continue     # asserted by its own test instead; see the dict for which and why
        o, x = r["original"], r["extracted"]
        excluded = _EXPECTED_DIVERGENCE(results["cases"][cid])
        o = json.loads(json.dumps(o))
        if excluded:
            o["decisions"]["active_count"] -= excluded
        if o != x:
            for part in ("trace", "state", "error", "decisions"):
                if o.get(part) != x.get(part):
                    diffs.append("%s: %s\n  original : %r\n  extracted: %r"
                                 % (cid, part, o.get(part), x.get(part)))
    assert not diffs, "the extraction changed behaviour:\n" + "\n".join(diffs)


def test_every_declared_behaviour_change_is_a_real_case(results):
    """A name in _DECLARED_BEHAVIOUR_CHANGES that matches no case would silently exempt nothing
    from the blanket comparison above -- catch a typo'd or removed id."""
    missing = set(_DECLARED_BEHAVIOUR_CHANGES) - set(results["cases"])
    assert not missing, missing


def test_the_declared_divergence_really_occurs(results):
    """A declared difference that no case exhibits is a claim nobody checked."""
    hit = [cid for cid, k in results["cases"].items() if _EXPECTED_DIVERGENCE(k)]
    assert hit, "no case exercises the stuck-worker divergence"
    for cid in hit:
        o = results["got"][cid]["original"]["decisions"]["active_count"]
        x = results["got"][cid]["extracted"]["decisions"]["active_count"]
        assert x < o, (cid, o, x)


def test_no_case_crashed_on_either_side(results):
    crashed = {cid: (r["original"]["error"], r["extracted"]["error"])
               for cid, r in results["got"].items()
               if r["original"]["error"] or r["extracted"]["error"]}
    assert not crashed, crashed


# ── orchestration: what the extracted flow does, case by case ─────────────────────────────

def test_nothing_is_done_for_empty_or_disabled_input(results):
    for cid in ("empty", "whitespace", "send_disabled"):
        assert _x(results, cid)["trace"] == [], (cid, _x(results, cid)["trace"])


def test_help_answers_locally_and_never_reaches_the_bridge(results):
    for cid in ("help", "help_upper_padded"):
        tr = _x(results, cid)["trace"]
        assert tr[0] == "ClearInput" and tr[-1] == "AddAssistant|HELP", tr
        assert not _gets(_x(results, cid)) and not _payloads(_x(results, cid)), tr


@pytest.mark.parametrize("cid,door", [
    ("door_new", "HttpGet|/new|15000"),
    ("door_new_null_url", "HttpGet|/new|15000"),
    ("door_switch", "HttpGet|/switch?url=https%3A%2F%2Fm365.cloud.microsoft%2Fchat%2F"
                    "conversation%2Fabc%3Fx%3D1%26y%3D2|15000"),
    ("door_resume_guid", "HttpGet|/resume?guid=11111111-2222-3333-4444-555555555555|30000"),
    ("door_sess_upper_is_a_url", "HttpGet|/switch?url=SESS%3Aabc|15000"),
    ("door_sid", "HttpGet|/resume?sid=sid-123|20000"),
    ("door_page_null", "HttpGet|/switch?url=https%3A%2F%2Fm365.cloud.microsoft%2Fchat%2F"
                       "conversation%2Fabc%3Fx%3D1%26y%3D2|15000"),
])
def test_each_conversation_shape_opens_exactly_one_door(results, cid, door):
    out = _x(results, cid)
    assert _gets(out) == [door], _gets(out)
    tr = out["trace"]
    i = tr.index(door)
    assert tr[i + 1] == "SetPageConv|c1", "the page was not pinned right after the door opened"
    assert tr[-1] == "BeginStream|%s|c1" % results["cases"][cid]["input"].strip(), tr
    assert out["state"]["page"] == "c1"


def test_a_conversation_already_on_the_page_opens_no_door(results):
    out = _x(results, "door_on_page")
    assert not _gets(out)
    assert out["trace"][-1] == "BeginStream|hello|c1"


def test_a_conversation_with_no_identity_is_refused_and_nothing_is_sent(results):
    for cid in ("door_unknown", "door_unknown_source_empty_with_name"):
        out = _x(results, cid)
        assert _said(out) == ["T:send_unknown_conv"], out["trace"]
        assert not _gets(out) and not any(ln.startswith("BeginStream") for ln in out["trace"])


def test_a_door_that_fails_says_so_and_does_not_send(results):
    for cid in ("door_resume_fails", "door_new_fails"):
        out = _x(results, cid)
        assert out["trace"][-1] == "AddAssistant|T:send_wrong_page", out["trace"]
        assert not any(ln.startswith(("BeginStream", "SetPageConv", "SendInFlight"))
                       for ln in out["trace"]), out["trace"]
        assert out["state"]["convs"]["c1"]["messages"] == [], "a failed send left a message"


def test_a_sent_message_is_recorded_titled_and_listed(results):
    out = _x(results, "door_long_title")
    c = out["state"]["convs"]["c1"]
    assert c["messages"] == ["U:" + "x" * 60 + "\nsecond"]
    assert c["title"] == "x" * 40 + "…"
    assert _x(results, "door_titled_kept")["state"]["convs"]["c1"]["title"] == "kept title"
    assert _x(results, "door_not_listed")["state"]["all"][0] == "c1"


def test_with_no_conversation_a_new_one_is_made_and_used(results):
    out = _x(results, "no_conv")
    assert out["trace"][0] == "NewChat"
    assert out["trace"][-1] == "BeginStream|hello|new1"
    assert out["state"]["current"] == "new1"


def test_an_unreachable_bridge_refuses_keeps_the_text_and_offers_the_fix(results):
    out = _x(results, "offline_stays_down")
    assert out["trace"] == [
        "HttpGet|/conv|5000", "SetBridgeReachable|false", "SetDot|offline",
        "AddAssistant|T:send_offline", "RestoreInput|hello there",
        "StartStackBanner|T:send_offline|T:retry_start_stack"], out["trace"]
    assert out["state"]["input"] == "hello there"
    back = _x(results, "offline_recovers")["trace"]
    assert back[:3] == ["HttpGet|/conv|5000", "SetBridgeReachable|true", "RefreshIdleDot"], back
    assert back[-1] == "BeginStream|hello there|c1"


def test_at_capacity_a_plain_message_is_queued_not_sent(results):
    out = _x(results, "cap_queued")
    assert _payloads(out) == [("add_goal", {"text": "plain words", "priority": False})]
    assert _said(out) == ["T:fleet_queued"]
    assert not _gets(out), "an at-capacity send still reached the bridge"
    tr = out["trace"]
    assert tr.index("ClearInput") < tr.index("HideRouter") < tr.index(
        'AppendCommand|add_goal|{"text":"plain words","priority":false}') < tr.index(
        "AddUser|plain words"), tr


def test_bang_forces_priority_and_is_stripped(results):
    out = _x(results, "cap_forced")
    assert _payloads(out) == [("add_goal", {"text": "urgent fix", "priority": True})]
    assert _said(out) == ["T:fleet_forced"]
    assert "AddUser|!  urgent fix" in out["trace"], "the person's own line was rewritten"


def test_bang_alone_at_capacity_does_nothing_at_all(results):
    for cid in ("cap_bang_alone", "cap_bang_space"):
        out = _x(results, cid)
        assert out["trace"] == ["ReadStatus"], out["trace"]
        assert out["state"]["input"] == results["cases"][cid]["input"], "the text was lost"


def test_bang_alone_with_room_is_just_a_message(results):
    assert _x(results, "bang_alone_with_room")["trace"][-1] == "BeginStream|!|c1"


def test_a_slash_command_is_never_rerouted(results):
    for cid, text in (("cap_slash_not_rerouted", "/goal a new job"),
                      ("cap_research_slash", "/research compare x")):
        out = _x(results, cid)
        assert not _payloads(out), out["trace"]
        assert out["trace"][-1] == "BeginStream|%s|c1" % text


def test_the_capacity_reading_follows_the_status_file(results):
    queued = {cid for cid in ("cap_bom", "cap_strings", "cap_idle_key", "cap_bad_bool",
                              "cap_no_running", "cap_maxc_zero", "cap_fraction", "cap_locked")
              if _payloads(_x(results, cid))}
    # BOM is stripped; "true"/"3" convert; everything else is "no fleet at capacity"
    assert queued == {"cap_bom", "cap_strings", "cap_fraction"}, queued


def test_a_failed_capacity_enqueue_is_reported_and_keeps_the_text(results):
    """FIXED 2026-09-24: the queue path used to ignore AppendCommand's result, so a command
    that was never written still said "queued" and the composer was already cleared -- the
    text was gone and never reached the fleet either. Now the write is checked: on failure the
    window says fleet_send_failed (the key the fleet-conversation path already uses for this)
    and puts the typed text back."""
    out = _x(results, "cap_append_fails_is_reported")
    # AppendCommand IS attempted (and recorded) -- it is its FAILURE that must now be reported,
    # rather than the old behaviour of trying, ignoring the result, and saying "queued" anyway.
    assert _payloads(out) == [("add_goal", {"text": "plain words", "priority": False})], out["trace"]
    assert _said(out) == ["T:fleet_send_failed"], out["trace"]
    assert out["state"]["input"] == "plain words", "the text was lost on a failed enqueue"


def test_at_capacity_a_fleet_conversation_uses_its_own_path(results):
    """FIXED 2026-09-24: capacity used to be decided before the fleet door, so a follow-up
    typed into a fleet conversation while the fleet was full became a NEW, unlinked bare goal
    -- no resume_conv, no follow_up_to -- and a steer-worthy message never reached the worker it
    was meant to steer. Now a fleet conversation skips the capacity reroute entirely and goes
    through SendToFleetConversation, exactly as it would if the fleet were not at capacity."""
    # a live worker (w0) matches this conversation's transcript -> steered, not queued
    steer = _x(results, "cap_fleet_steer_not_rerouted")
    assert _payloads(steer) == [("steer", {"worker": "w0", "text": "a follow-up"})], steer["trace"]
    assert _said(steer) == ["T:fleet_steer_sent"]
    assert not _gets(steer), "a fleet conversation went through a page door"

    # no worker matches this conversation's transcript -> a follow-up goal, not a bare one
    follow = _x(results, "cap_fleet_follow_up_not_rerouted")
    want_text = ("【ユーザーからの追加指示】a follow-up\n直前までの作業内容を踏まえ、この追加指示に対して"
                 "だけ答えてください。最初からやり直す必要はありません。完了なら DONE、無理なら FAIL と"
                 "理由を書いてください。")
    assert _payloads(follow) == [("add_goal", {
        "text": want_text, "resume_conv": SESS,
        "follow_up_to": "summarise the release notes", "priority": True})], follow["trace"]
    assert _said(follow) == ["T:fleet_follow_sent"]
    assert not _gets(follow), "a fleet conversation went through a page door"


def test_research_intent_asks_first(results):
    for cid, text in (("research_en", "please investigate the outage"),
                      ("research_ja", "これを調査して")):
        out = _x(results, cid)
        assert out["trace"][-1] == "ShowRouter|" + text, out["trace"]
        assert not _gets(out) and not _payloads(out)
    assert _x(results, "research_router_already_shown")["trace"][-1].startswith("BeginStream|")
    assert _x(results, "research_slash")["trace"][-1].startswith("BeginStream|")
    assert _x(results, "router_button_research")["trace"][-1] == \
        "BeginStream|/research please investigate|c1"


# ── the fleet conversation ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cid,worker", [
    ("fleet_steer_live", "w0"),
    ("fleet_steer_idle_key_still_live", "w0"),
    ("fleet_steer_upper_done_is_live", "w0"),
    ("fleet_steer_case_path", "w0"),
    ("fleet_dup_live_first", "w0"),
])
def test_a_live_worker_gets_a_steer(results, cid, worker):
    out = _x(results, cid)
    text = results["cases"][cid]["input"]
    assert _payloads(out) == [("steer", {"worker": worker, "text": text})], out["trace"]
    assert _said(out) == ["T:fleet_steer_sent"]
    assert not _gets(out), "a fleet conversation went through a page door"


def test_the_word_goal_alone_is_not_the_verb(results):
    """DoSend trims, so "/goal" never carries the "/goal " prefix; it is steered as text."""
    out = _x(results, "fleet_bare_goal_word_steers")
    assert _payloads(out) == [("steer", {"worker": "w0", "text": "/goal"})]


def test_a_steer_that_cannot_be_written_says_so(results):
    out = _x(results, "fleet_steer_append_fails")
    assert _said(out) == ["T:fleet_send_failed"], out["trace"]


@pytest.mark.parametrize("cid", ["fleet_goal_while_live", "fleet_goal_upper"])
def test_goal_while_live_is_a_new_goal_not_a_steer(results, cid):
    out = _x(results, cid)
    assert _payloads(out) == [("add_goal", {
        "text": "write the changelog", "resume_conv": SESS,
        "follow_up_to": "summarise the release notes", "priority": True, "new_task": True})]
    assert _said(out) == ["T:fleet_follow_sent"]


def test_goal_with_nothing_after_it_is_refused(results):
    out = _x(results, "fleet_goal_empty_via_router")
    assert not _payloads(out) and _said(out) == ["T:fleet_goal_empty"], out["trace"]


def test_goal_on_a_conversation_with_no_goal_text_is_refused(results):
    out = _x(results, "fleet_goal_no_goal_text")
    assert not _payloads(out) and _said(out) == ["T:fleet_no_goal"], out["trace"]


_JA = ("【ユーザーからの追加指示】and the dates?\n直前までの作業内容を踏まえ、この追加指示に対して"
       "だけ答えてください。最初からやり直す必要はありません。完了なら DONE、無理なら FAIL と"
       "理由を書いてください。")
_EN = ("[follow-up from the user] and the dates?\nAnswer only this follow-up, building on the "
       "work so far. Do not start over. Write DONE when finished, or FAIL and why.")


@pytest.mark.parametrize("cid,text,resume,said", [
    ("fleet_follow_ja_running", _JA, SESS, "T:fleet_follow_sent"),
    ("fleet_follow_en_idle", _EN, SESS, "T:fleet_follow_idle"),
    ("fleet_follow_pending_is_not_live", _JA, SESS, "T:fleet_follow_sent"),
    ("fleet_follow_finished", _JA, SESS, "T:fleet_follow_idle"),
    ("fleet_dup_dead_first", _JA, SESS, "T:fleet_follow_sent"),
    ("fleet_follow_no_transcript", _JA, SESS, "T:fleet_follow_sent"),
    ("fleet_follow_malformed", _JA, SESS, "T:fleet_follow_idle"),
    ("fleet_follow_locked", _JA, SESS, "T:fleet_follow_idle"),
    ("fleet_follow_sess_upper", _JA, "SESS:abc-def", "T:fleet_follow_idle"),
    ("fleet_follow_no_guid", _JA, None, "T:fleet_follow_idle"),
    ("fleet_follow_null_url", _JA, None, "T:fleet_follow_idle"),
    ("fleet_follow_url_not_sess", _JA, None, "T:fleet_follow_idle"),
    ("fleet_on_page_still_fleet", _JA, SESS, "T:fleet_follow_idle"),
])
def test_a_follow_up_carries_the_conversation_and_the_framing(results, cid, text, resume, said):
    out = _x(results, cid)
    want = {"text": text}
    if resume is not None:
        want["resume_conv"] = resume
    want.update({"follow_up_to": "summarise the release notes", "priority": True})
    got = _payloads(out)
    assert got == [("add_goal", want)], got
    assert list(got[0][1]) == list(want), "key order changed: %r" % list(got[0][1])
    assert _said(out) == [said], out["trace"]
    assert not _gets(out)


def test_a_follow_up_that_cannot_be_written_says_failed_and_never_sent(results):
    out = _x(results, "fleet_follow_append_fails")
    assert _said(out) == ["T:fleet_send_failed"], out["trace"]
    assert out["trace"][-1] == "StickToEnd"
    assert "ReadStatus" not in out["trace"][out["trace"].index("AddAssistant|T:fleet_send_failed"):]


def test_no_goal_text_means_no_follow_up(results):
    for cid in ("fleet_no_goal_empty", "fleet_no_goal_spaces", "fleet_no_goal_null"):
        out = _x(results, cid)
        assert not _payloads(out) and _said(out) == ["T:fleet_no_goal"], (cid, out["trace"])


def test_the_research_router_runs_before_the_fleet_door(results):
    assert _x(results, "fleet_research_router_first")["trace"][-1] == \
        "ShowRouter|please investigate why"


def test_the_fleet_path_records_the_line_before_it_decides(results):
    tr = _x(results, "fleet_steer_live")["trace"]
    assert tr[:6] == ["ReadStatus", "HideRouter", "ClearInput", "ClearInput", "HideRouter",
                      "AddUser|keep going but shorter"], tr
    assert tr[6] == "ReadStatus", "the live-worker lookup did not read status.json"
    assert _x(results, "fleet_steer_live")["state"]["convs"]["f1"]["messages"][-1] == \
        "U:keep going but shorter"


def test_every_status_is_classified_for_the_steer(results):
    live = {s for s in _ALL_STATUSES if _payloads(_x(results, "status_" + _slug(s)), "steer")}
    # case-SENSITIVE: "Stuck"/"Done" are live to the steer lookup; "maxturns" and
    # "content_refused" are terminal (as of 2026-09-24) and so is neither
    assert live == {"running", "verifying", "Stuck", "Done"}, sorted(live)


def test_a_now_terminal_worker_is_not_steered(results):
    """FIXED 2026-09-24: IsTerminalWorkerStatus gained "maxturns" and "content_refused" (relay's
    own terminal set already had both). A worker in either status is finished and will never
    read a steer, so the message must become a follow-up goal instead of being lost."""
    for cid, status in (("status_maxturns", "maxturns"),
                        ("status_content_refused", "content_refused")):
        out = _x(results, cid)
        assert not _payloads(out, "steer"), (status, out["trace"])
        payloads = _payloads(out, "add_goal")
        assert len(payloads) == 1, (status, out["trace"])
        item = payloads[0][1]
        assert item["text"].startswith("【ユーザーからの追加指示】anything\n"), (status, item)
        assert item["resume_conv"] == SESS and item["follow_up_to"] == "summarise the release notes"
        assert _said(out) == ["T:fleet_follow_sent"], (status, out["trace"])


def test_the_chip_count_no_longer_counts_a_stuck_worker(results):
    d = _x(results, "status_running")["decisions"]
    # 14 workers; not active: done resolved failed error cancelled stopped stuck pending
    # maxturns content_refused Stuck Done -- 12 excluded, 2 left
    assert d["active_count"] == 2, d            # running, verifying


# ── cross-language: the command the window wrote is the goal the fleet reads ──────────────

def test_every_command_written_reads_back_through_the_fleets_own_reader(results):
    from relay import fleet_runner as FR

    checked = 0
    for side in ("extracted", "original"):
        for cid, r in sorted(results["got"].items()):
            sent = _payloads(r[side])
            if not sent or not results["cases"][cid].get("append_ok", True):
                continue
            cmds = FR.read_commands(os.path.join(results["work"], side, cid))
            assert len(cmds) == len(sent), (side, cid, cmds, sent)
            for (key, item), cmd in zip(sent, cmds):
                assert list(cmd) == [key] and cmd[key] == [item], (side, cid, cmd)
                if key != "add_goal":
                    continue
                goals = FR.goals_from_command(cmd)
                assert len(goals) == 1, (side, cid, goals)
                g = goals[0]
                assert g["text"] == item["text"], (side, cid)
                assert g["priority"] is item["priority"], (side, cid, g)
                for field in ("resume_conv", "follow_up_to", "new_task"):
                    assert (field in g) == (field in item), (side, cid, field, g, item)
                    assert g.get(field) == item.get(field), (side, cid, field)
                checked += 1
    assert checked >= 20, "only %d add_goal round trips were checked" % checked


def test_the_status_fixtures_came_from_the_real_writer(results):
    """Guard against the fixtures quietly becoming hand-written: the REAL ones must carry
    fields only fleet_runner._snapshot writes."""
    root = os.path.dirname(results["work"])
    for key in REAL:
        with open(os.path.join(root, "status", key, "status.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        assert "quota" in d and "pending_gates" in d and "run_id" in d["workers"][0], key
