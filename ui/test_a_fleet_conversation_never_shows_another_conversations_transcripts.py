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
(sess:<guid>, the transcript's own guid line); with no id it is the transcript file's stem
(r<run>_a<attempt>_w<N>). A worker name is never identity. The source-text tests at the end pin that the chat window
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
    base = dict(op="registry_match", url="", anchor="", row_url="", row_transcript="", row_transcripts=[])
    base.update(kw)
    return base


def _sync(**kw):
    base = dict(op="sync_match", row_url="", row_transcript="", row_transcripts=[],
                c_url="", c_transcript="", c_transcripts=[])
    base.update(kw)
    return base


@needs_csc
def test_a_stem_is_the_identity_of_a_conversation_with_no_id(run_cases):
    got = run_cases([
        {"id": "a", "op": "stem_of", "path": PHONE_A0},
        {"id": "b", "op": "stem_of", "path": PHONE_A1},
        {"id": "c", "op": "stem_of", "path": TD + r"\R6ADAC45A_a0_w1.JSONL"},
        {"id": "k_id", "op": "identity_key", "url": ADAMS_URL, "path": PHONE_A0},
        {"id": "k_stem", "op": "identity_key", "url": "", "path": PHONE_A1},
        {"id": "k_none", "op": "identity_key", "url": "", "path": ""},
    ])
    assert got["a"] == "r6acf1938_a0_w1"
    assert got["b"] == "r6acf1938_a1_w1"
    assert got["c"] == "r6adac45a_a0_w1"
    assert got["k_id"] == ADAMS_URL
    assert got["k_stem"] == "stem:r6acf1938_a1_w1"
    assert got["k_none"] == ""


@needs_csc
def test_opening_adams_does_not_take_a_transcript_that_carries_another_id(run_cases):
    """OpenFromFleet(url=sess:ADAMS, worker=w1): a transcript with another Copilot id never joins."""
    d = run_cases.dir
    mine = _write_transcript(d / "r6adab543_a0_w1.jsonl", ADAMS_URL[5:])
    other = _write_transcript(d / "r6adac45a_a0_w1.jsonl", "00000002-aaaa-bbbb-cccc-000000000002")
    other_gz = _write_transcript(d / "r6adac45a_a1_w1.jsonl.gz", "00000002-aaaa-bbbb-cccc-000000000002", gz=True)
    pending = _write_transcript(d / "r6adae000_a0_w1.jsonl", "")   # no guid value yet
    got = run_cases([
        {"id": "own_hint", "op": "hint_may_join", "url": ADAMS_URL, "hint_path": mine},
        {"id": "foreign_hint", "op": "hint_may_join", "url": ADAMS_URL, "hint_path": other},
        {"id": "foreign_hint_gz", "op": "hint_may_join", "url": ADAMS_URL, "hint_path": other_gz},
        {"id": "unknown_guid_hint", "op": "hint_may_join", "url": ADAMS_URL, "hint_path": pending},
        {"id": "missing_hint", "op": "hint_may_join", "url": ADAMS_URL, "hint_path": str(d / "nope.jsonl")},
        {"id": "no_id_any_hint", "op": "hint_may_join", "url": "", "hint_path": other},
        {"id": "guid_gz", "op": "transcript_guid", "path": other_gz},
        {"id": "guid_gz_by_plain_name", "op": "transcript_guid", "path": str(d / "r6adac45a_a1_w1.jsonl")},
    ])
    assert got["own_hint"] is True
    assert got["foreign_hint"] is False, "another conversation's transcript joined ADAMS' lineage"
    assert got["foreign_hint_gz"] is False
    assert got["unknown_guid_hint"] is True, "a live run whose guid is not written yet must stay openable"
    assert got["missing_hint"] is True
    assert got["no_id_any_hint"] is True
    assert got["guid_gz"] == "00000002-aaaa-bbbb-cccc-000000000002"
    assert got["guid_gz_by_plain_name"] == "00000002-aaaa-bbbb-cccc-000000000002"


@needs_csc
def test_the_adams_shape_from_2026_10_23(run_cases):
    """The real case: ADAMS (id, w1) against the url-less 10-14 w1 rows and today's w1 row."""
    adams = dict(row_url=ADAMS_URL, row_transcript=ADAMS_T[-1], row_transcripts=ADAMS_T)
    phone = dict(row_url="", row_transcript=PHONE_A0, row_transcripts=[PHONE_A0])
    cur = dict(row_url="", row_transcript=NOW_W1, row_transcripts=[NOW_W1])
    adams_c = dict(c_url=ADAMS_URL, c_transcript=ADAMS_T[-1], c_transcripts=ADAMS_T)
    got = run_cases([
        _reg(id="open_adams_finds_its_row", url=ADAMS_URL, anchor="", **adams),
        _reg(id="open_adams_not_phone_row", url=ADAMS_URL, anchor="", **phone),
        _reg(id="open_adams_not_todays_row", url=ADAMS_URL, anchor="", **cur),
        _reg(id="live_no_id_finds_own_row", url="", anchor=NOW_W1, **cur),
        _reg(id="live_no_id_not_old_row", url="", anchor=NOW_W1, **phone),
        _reg(id="no_id_no_anchor_matches_nothing", url="", anchor="", **cur),
        _reg(id="same_file_gz", url="", anchor=NOW_W1 + ".gz", **cur),
        _sync(id="sync_phone_vs_adams", row_transcript=PHONE_A0, row_transcripts=[PHONE_A0], **adams_c),
        _sync(id="sync_phone_gz_vs_adams", row_transcript=PHONE_A1, row_transcripts=[PHONE_A1], **adams_c),
        _sync(id="sync_own_file_row", row_transcript=TD + r"\r6adab543_a0_w1.jsonl.gz", **adams_c),
        _sync(id="sync_id_row_by_id", row_url=ADAMS_URL, row_transcript="", **adams_c),
        _sync(id="sync_other_id_row", row_url="sess:00000002-aaaa-bbbb-cccc-000000000002",
              row_transcript=ADAMS_T[-1], **adams_c),
    ])
    assert got["open_adams_finds_its_row"] is True
    assert got["open_adams_not_phone_row"] is False
    assert got["open_adams_not_todays_row"] is False, "a url-less row matched an id'd conversation"
    assert got["live_no_id_finds_own_row"] is True, "a live conversation with no id must stay openable"
    assert got["live_no_id_not_old_row"] is False
    assert got["no_id_no_anchor_matches_nothing"] is False
    assert got["same_file_gz"] is True
    assert got["sync_phone_vs_adams"] is False, "the 10-14 phone-ledger row was merged into ADAMS"
    assert got["sync_phone_gz_vs_adams"] is False
    assert got["sync_own_file_row"] is True
    assert got["sync_id_row_by_id"] is True
    assert got["sync_other_id_row"] is False


def _population(n_ids=8, n_noid=4):
    """Many conversations, ALL on worker w0, different ids; some have no id at all. Each has one
    to three transcript files (follow-ups), all named ..._w0."""
    convs = []
    for i in range(n_ids + n_noid):
        files = [TD + r"\r6b%05x_a%d_w0.jsonl" % (i, k) for k in range(1 + i % 3)]
        convs.append({
            "i": i,
            "url": ("sess:%08x-0000-4000-8000-%012x" % (i + 1, i + 1)) if i < n_ids else "",
            "files": files,
        })
    return convs


@needs_csc
def test_many_conversations_on_the_same_worker_name_never_see_each_other(run_cases):
    """12 conversations, every one on w0, 8 with a Copilot id and 4 with none. Opening any of them
    selects its own registry row and no other; the registry poll maps every row onto its own
    sidebar row and no other. A no-id conversation is found by its transcript file only."""
    convs = _population()
    cases = []
    for o in convs:                      # o = the conversation being opened
        for r in convs:                  # r = a registry row
            cases.append(_reg(id="reg-%d-%d" % (o["i"], r["i"]), url=o["url"], anchor=o["files"][-1],
                              row_url=r["url"], row_transcript=r["files"][-1], row_transcripts=r["files"]))
            cases.append(_sync(id="sync-%d-%d" % (o["i"], r["i"]), row_url=r["url"],
                               row_transcript=r["files"][-1], row_transcripts=r["files"],
                               c_url=o["url"], c_transcript=o["files"][-1], c_transcripts=o["files"]))
    got = run_cases(cases)
    for o in convs:
        for r in convs:
            want = o["i"] == r["i"]
            assert got["reg-%d-%d" % (o["i"], r["i"])] is want, ("registry lookup", o["i"], r["i"])
            assert got["sync-%d-%d" % (o["i"], r["i"])] is want, ("registry poll", o["i"], r["i"])
    assert sum(1 for c in convs if not c["url"]) >= 4


@needs_csc
def test_a_conversation_with_no_id_is_found_by_its_stem_whichever_spelling_the_file_has(run_cases):
    f = TD + r"\r6b00009_a0_w0.jsonl"
    got = run_cases([
        _reg(id="gz_anchor", url="", anchor=f + ".gz", row_transcript=f, row_transcripts=[f]),
        _reg(id="gz_row", url="", anchor=f, row_transcript=f + ".gz", row_transcripts=[f + ".gz"]),
        _reg(id="other_worker_same_run", url="", anchor=TD + r"\r6b00009_a0_w1.jsonl",
             row_transcript=f, row_transcripts=[f]),
        _reg(id="other_attempt_same_worker", url="", anchor=TD + r"\r6b00009_a1_w0.jsonl",
             row_transcript=f, row_transcripts=[f]),
    ])
    assert got["gz_anchor"] is True
    assert got["gz_row"] is True
    assert got["other_worker_same_run"] is False
    assert got["other_attempt_same_worker"] is False


# -- the chat window calls those functions and compares no bare name --------------------------

def _code() -> str:
    text = ui_source.chat_window_source(encoding="utf-8-sig")
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in text.splitlines())


def _block(code: str, start: str, length: int) -> str:
    i = code.index(start)
    return code[i:i + length]


def test_no_lookup_by_worker_name_is_left_in_the_chat_window():
    code = _code()
    assert "NewestTranscriptForWorker" not in code, (
        "'the newest transcript of worker w1' is some other run's conversation")
    assert "MayResolveByWorkerName" not in code


def test_open_from_fleet_anchors_on_a_transcript_and_keys_a_url_less_row_by_its_stem():
    body = _block(_code(), "void OpenFromFleet(string url, string worker, string transcriptHint)", 4200)
    assert "FleetConvIdentity.HintMayJoin(url, ConvListing.TranscriptGuid(liveTranscriptPath))" in body
    assert "RegistryTranscriptLineageFor(url, lineageAnchor, liveTranscriptPath)" in body
    assert '"fleet:" + FleetConvIdentity.StemOf(lineageAnchor)' in body, (
        "a url-less row was keyed 'fleet:<worker name>', shared by every run's worker of that name")
    assert "StemOf(transcriptHint)" in body


def test_the_registry_lookups_go_through_the_identity_matchers():
    code = _code()
    lin = _block(code, "List<string> RegistryTranscriptLineageFor(", 1700)
    assert "FleetConvIdentity.RegistryRowMatches(" in lin
    assert 'dn == worker' not in lin and 'SS(d, "name")' not in lin
    sync = _block(code, "void SyncRegistry()", 5200)
    assert "FleetConvIdentity.SyncRowMatches(" in sync
    assert "regName == c.Name" not in sync


def test_a_status_worker_is_found_by_stem_as_well_as_by_current_name():
    body = _block(_code(), "Dictionary<string, object> ReadFleetWorker(string key)", 1800)
    assert "FleetConvIdentity.StemOf(SS(w, \"transcript\")) == wantStem" in body
