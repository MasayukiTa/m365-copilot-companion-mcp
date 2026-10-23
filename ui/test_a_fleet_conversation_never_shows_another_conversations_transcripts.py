# -*- coding: utf-8 -*-
"""A fleet conversation opened in the main chat shows its OWN transcripts, never another one's.

MEASURED 2026-10-23: the ADAMS conversation (sess:65c85c0a-..., worker w1) was opened from the
cockpit and the chat showed, after its own turns, two 10-14 phone-ledger transcripts
(r6acf1938_a0_w1, r6acf1938_a1_w1) and then the current run's computer-use transcript
(r6adac45a_a0_w1). Nothing connected them to ADAMS except the worker name "w1", which every run
reuses. Three places compared that name:

  * OpenFromFleet took "the newest transcript of worker w1" as the conversation's latest
    transcript when status.json had no worker for the id (computer-use run appended).
  * RegistryTranscriptLineageFor matched a registry row with no id by (source, name) alone.
  * SyncRegistry matched a registry row with no id to whichever sidebar row had the same
    (source, name), and merged that row's transcripts into it (phone ledger appended).

The decisions now live in ui/FleetConvIdentity.cs (WPF-free) and this test compiles and runs
them with ui/harness/ConvMixHarness.cs. A conversation's identity is its Copilot id
(sess:<guid>, the transcript's own guid line); a worker name counts only together with the same
RUN (file-name prefix before _w<N>). The source-text tests at the end pin that the chat window
calls these functions and no longer compares a bare name.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc, ui_source  # noqa: E402

UI = os.path.join(REPO, "ui")
FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")
SOURCES = [
    os.path.join(UI, "FleetConvIdentity.cs"),
    os.path.join(UI, "harness", "ConvMixHarness.cs"),
]

needs_csc = pytest.mark.skipif(
    os.name != "nt", reason="the code under test is C# compiled by the .NET Framework csc (Windows only)")

# The shape of the real data of 2026-10-23, with the ids made up.
TD = r"C:\repo\.fleet\transcripts"
ADAMS_URL = "sess:00000001-aaaa-bbbb-cccc-000000000001"
ADAMS_T = [TD + r"\r6adab187_a0_w0.jsonl", TD + r"\r6adab543_a0_w0.jsonl", TD + r"\r6adab543_a0_w1.jsonl"]
PHONE_A0 = TD + r"\r6acf1938_a0_w1.jsonl"          # 10-14, url-less registry row, name w1
PHONE_A1 = TD + r"\r6acf1938_a1_w1.jsonl.gz"       # same, compressed
NOW_W1 = TD + r"\r6adac45a_a0_w1.jsonl"            # the current run's w1 (another conversation)


def _require_csc():
    return os.environ.get("REQUIRE_CSC", "").strip() == "1"


def _write_transcript(path, guid, gz=False):
    lines = [json.dumps({"meta": True, "name": "w1", "goal": "g"}),
             json.dumps({"turn": 1, "role": "user", "text": "hello"}),
             json.dumps({"guid": guid})]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if gz:
        with gzip.open(str(path), "wb") as fh:
            fh.write(data)
    else:
        with open(str(path), "wb") as fh:
            fh.write(data)
    return str(path)


@pytest.fixture(scope="module")
def run_cases(tmp_path_factory):
    if not os.path.isfile(CSC):
        msg = "csc.exe is not at %s" % CSC
        if _require_csc():
            pytest.fail(msg + " and REQUIRE_CSC=1")
        pytest.skip(msg)
    d = tmp_path_factory.mktemp("conv_mix")
    exe = str(d / "ConvMixHarness.exe")
    r = childproc.run([CSC, "/nologo", "/target:exe", "/out:" + exe,
                       "/r:" + os.path.join(FW, "System.Web.Extensions.dll")] + SOURCES, timeout=300)
    assert r.returncode == 0 and os.path.isfile(exe), (
        "csc could not build FleetConvIdentity.cs with the harness (rc=%s):\n%s\n%s"
        % (r.returncode, r.stdout, r.stderr))
    state = {"dir": d, "n": 0}

    def run(cases):
        state["n"] += 1
        cpath = str(d / ("cases%d.json" % state["n"]))
        rpath = str(d / ("results%d.json" % state["n"]))
        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(cases, fh, ensure_ascii=False)
        p = childproc.run([exe, "run", cpath, rpath], timeout=180)
        assert p.returncode == 0, "harness failed (rc=%s):\n%s\n%s" % (p.returncode, p.stdout, p.stderr)
        with open(rpath, encoding="utf-8") as fh:
            got = json.load(fh)
        for k, v in got.items():
            assert not v["error"], (k, v["error"])
        return {k: v["value"] for k, v in got.items()}

    run.dir = d
    return run


def _reg(**kw):
    base = dict(op="registry_match", url="", worker="", hint="", row_url="", row_source="fleet",
                row_name="", row_transcript="", row_transcripts=[])
    base.update(kw)
    return base


def _sync(**kw):
    base = dict(op="sync_match", row_url="", row_source="fleet", row_name="", row_transcript="",
                row_transcripts=[], c_url="", c_source="fleet", c_name="", c_transcript="",
                c_transcripts=[])
    base.update(kw)
    return base


@needs_csc
def test_run_of_names_the_run_not_the_worker(run_cases):
    got = run_cases([
        {"id": "a", "op": "run_of", "path": PHONE_A0},
        {"id": "b", "op": "run_of", "path": PHONE_A1},
        {"id": "c", "op": "run_of", "path": TD + r"\r6adac45a_a0_w1__sub_research_t1_1.jsonl"},
        {"id": "d", "op": "run_of", "path": TD + r"\old_name.jsonl"},
    ])
    assert got["a"] == "r6acf1938_a0"
    assert got["b"] == "r6acf1938_a1"
    assert got["c"] == "r6adac45a_a0"
    assert got["d"] == ""


@needs_csc
def test_opening_adams_does_not_take_the_newest_w1_of_another_run(run_cases):
    """OpenFromFleet(url=sess:ADAMS, worker=w1): the worker-name fallback must be off whenever
    the conversation has an id, and a hint carrying another Copilot id must not join."""
    d = run_cases.dir
    mine = _write_transcript(d / "r6adab543_a0_w1.jsonl", ADAMS_URL[5:])
    other = _write_transcript(d / "r6adac45a_a0_w1.jsonl", "00000002-aaaa-bbbb-cccc-000000000002")
    other_gz = _write_transcript(d / "r6adac45a_a1_w1.jsonl.gz", "00000002-aaaa-bbbb-cccc-000000000002", gz=True)
    pending = _write_transcript(d / "r6adae000_a0_w1.jsonl", "")   # no guid line value yet
    got = run_cases([
        {"id": "name_with_id", "op": "may_resolve_by_name", "url": ADAMS_URL},
        {"id": "name_without_id", "op": "may_resolve_by_name", "url": ""},
        {"id": "own_hint", "op": "hint_may_join", "url": ADAMS_URL, "hint_path": mine},
        {"id": "foreign_hint", "op": "hint_may_join", "url": ADAMS_URL, "hint_path": other},
        {"id": "foreign_hint_gz", "op": "hint_may_join", "url": ADAMS_URL, "hint_path": other_gz},
        {"id": "unknown_guid_hint", "op": "hint_may_join", "url": ADAMS_URL, "hint_path": pending},
        {"id": "missing_hint", "op": "hint_may_join", "url": ADAMS_URL, "hint_path": str(d / "nope.jsonl")},
        {"id": "no_id_any_hint", "op": "hint_may_join", "url": "", "hint_path": other},
        {"id": "guid_gz", "op": "transcript_guid", "path": other_gz},
        {"id": "guid_gz_by_plain_name", "op": "transcript_guid", "path": str(d / "r6adac45a_a1_w1.jsonl")},
    ])
    assert got["name_with_id"] is False
    assert got["name_without_id"] is True
    assert got["own_hint"] is True
    assert got["foreign_hint"] is False, "another conversation's transcript joined ADAMS' lineage"
    assert got["foreign_hint_gz"] is False
    assert got["unknown_guid_hint"] is True, "a live run whose guid is not written yet must stay openable"
    assert got["missing_hint"] is True
    assert got["no_id_any_hint"] is True
    assert got["guid_gz"] == "00000002-aaaa-bbbb-cccc-000000000002"
    assert got["guid_gz_by_plain_name"] == "00000002-aaaa-bbbb-cccc-000000000002"


@needs_csc
def test_registry_lineage_lookup_matches_by_id_then_by_run_never_by_name(run_cases):
    adams_row = dict(row_url=ADAMS_URL, row_name="w1", row_transcript=ADAMS_T[-1], row_transcripts=ADAMS_T)
    phone_row = dict(row_url="", row_name="w1", row_transcript=PHONE_A0, row_transcripts=[PHONE_A0])
    cur_row = dict(row_url="", row_name="w1", row_transcript=NOW_W1, row_transcripts=[NOW_W1])
    got = run_cases([
        # by id
        _reg(id="id_hit", url=ADAMS_URL, worker="w1", hint=NOW_W1, **adams_row),
        # id given: a url-less row of another run is not this conversation, even with name w1
        _reg(id="id_vs_phone_row", url=ADAMS_URL, worker="w1", hint=NOW_W1, **phone_row),
        # id given: another conversation's row is not matched through the hint
        _reg(id="id_vs_hint_row", url=ADAMS_URL, worker="w1", hint=NOW_W1,
             row_url="sess:00000002-aaaa-bbbb-cccc-000000000002", row_name="w1",
             row_transcript=NOW_W1, row_transcripts=[NOW_W1]),
        # no id (live, url never captured): the same run matches...
        _reg(id="live_same_run", url="", worker="w1", hint=NOW_W1, **cur_row),
        # ...an earlier run with the same worker name does not
        _reg(id="live_other_run", url="", worker="w1", hint=NOW_W1, **phone_row),
        # ...and with nothing to anchor the run, a name alone matches nothing
        _reg(id="name_only", url="", worker="w1", hint="", **phone_row),
        # the exact transcript file still matches (compressed spelling too)
        _reg(id="same_file_gz", url="", worker="w1", hint=NOW_W1 + ".gz", **cur_row),
    ])
    assert got["id_hit"] is True
    assert got["id_vs_phone_row"] is False
    assert got["id_vs_hint_row"] is False
    assert got["live_same_run"] is True
    assert got["live_other_run"] is False, "a url-less row of another run matched by worker name"
    assert got["name_only"] is False
    assert got["same_file_gz"] is True


@needs_csc
def test_the_registry_poll_does_not_merge_another_runs_row_into_a_sidebar_row(run_cases):
    """SyncRegistry: the url-less phone-ledger rows (name w1) must not land on the ADAMS row
    (name w1); the url-less row of ADAMS' own run still does, and a live url-less row keeps
    updating its own sidebar row."""
    adams_c = dict(c_url=ADAMS_URL, c_name="w1", c_transcript=ADAMS_T[-1], c_transcripts=ADAMS_T)
    live_c = dict(c_url="fleet:w1", c_name="w1", c_transcript=NOW_W1, c_transcripts=[NOW_W1])
    got = run_cases([
        _sync(id="phone_vs_adams", row_name="w1", row_transcript=PHONE_A0, row_transcripts=[PHONE_A0], **adams_c),
        _sync(id="phone_gz_vs_adams", row_name="w1", row_transcript=PHONE_A1, row_transcripts=[PHONE_A1], **adams_c),
        _sync(id="own_run_vs_adams", row_name="w1", row_transcript=ADAMS_T[-1], row_transcripts=[ADAMS_T[-1]], **adams_c),
        _sync(id="same_run_other_file", row_name="w1", row_transcript=TD + r"\r6adab543_a0_w1.jsonl.gz",
              row_transcripts=[], **adams_c),
        _sync(id="live_row_updates", row_name="w1", row_transcript=NOW_W1, row_transcripts=[NOW_W1], **live_c),
        _sync(id="live_row_vs_old_run", row_name="w1", row_transcript=PHONE_A0, row_transcripts=[PHONE_A0], **live_c),
        _sync(id="id_row_by_id", row_url=ADAMS_URL, row_name="w9", row_transcript="", **adams_c),
        _sync(id="id_row_other_id", row_url="sess:00000002-aaaa-bbbb-cccc-000000000002", row_name="w1",
              row_transcript=ADAMS_T[-1], **adams_c),
        _sync(id="empty_name", row_name="", row_transcript="", row_transcripts=[], **adams_c),
    ])
    assert got["phone_vs_adams"] is False, "the 10-14 phone-ledger row was merged into the ADAMS conversation"
    assert got["phone_gz_vs_adams"] is False
    assert got["own_run_vs_adams"] is True
    assert got["same_run_other_file"] is True
    assert got["live_row_updates"] is True, "a live conversation must keep receiving its own registry row"
    assert got["live_row_vs_old_run"] is False
    assert got["id_row_by_id"] is True
    assert got["id_row_other_id"] is False
    assert got["empty_name"] is False


# ── the chat window calls those functions and compares no bare name ────────────────────────

def _code() -> str:
    text = ui_source.chat_window_source(encoding="utf-8-sig")
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in text.splitlines())


def _block(code: str, start: str, length: int) -> str:
    i = code.index(start)
    return code[i:i + length]


def test_open_from_fleet_uses_the_worker_name_only_for_a_key_without_an_id():
    body = _block(_code(), "void OpenFromFleet(string url, string worker, string transcriptHint)", 2600)
    assert "FleetConvIdentity.MayResolveByWorkerName(url)" in body
    assert "FleetConvIdentity.HintMayJoin(url, ConvListing.TranscriptGuid(liveTranscriptPath))" in body
    # the fallback call must sit behind the id check, not stand alone
    i = body.index("NewestTranscriptForWorker(worker)")
    assert "MayResolveByWorkerName(url)" in body[max(0, i - 200):i]


def test_the_registry_lookups_go_through_the_run_aware_matchers():
    code = _code()
    lin = _block(code, "List<string> RegistryTranscriptLineageFor(", 1700)
    assert "FleetConvIdentity.RegistryRowMatches(" in lin
    assert 'dn == worker' not in lin and "du.Length == 0 && ds ==" not in lin
    sync = _block(code, "void SyncRegistry()", 5200)
    assert "FleetConvIdentity.SyncRowMatches(" in sync
    assert "regName == c.Name" not in sync


def test_the_sidebar_open_and_the_follow_do_not_resolve_an_id_by_worker_name():
    code = _code()
    op = _block(code, "void OpenConversation(Conversation c)", 2200)
    assert "MayResolveByWorkerName(c.ConvUrl)" in op
    fol = _block(code, "void MaybeFollowConversation(", 1200)
    assert "MayResolveByWorkerName(key)" in fol
