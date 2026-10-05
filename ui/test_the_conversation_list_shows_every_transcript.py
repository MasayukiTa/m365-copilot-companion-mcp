# -*- coding: utf-8 -*-
"""The main chat's sidebar can reach EVERY conversation that is on disk, EXECUTED.

THE REPORT (2026-10-05): old conversations often cannot be opened from the main chat window.
Three silent limits, found and reproduced:

  1. The sidebar kept the newest 80 transcripts, scanned once at startup (`int budget = 80`).
     3,309 were on disk and all of them were in the session database.
  2. .fleet/conversations.json grew past 2,097,152 characters -- JavaScriptSerializer's default
     MaxJsonLength. Deserialising threw, a bare `catch { }` swallowed it, and the registry
     contributed an empty list. The same ceiling applied to each transcript line.
  3. The registry pruned a row 24 h after it stopped being linked to a session, even while the
     transcript file was still on disk (relay/test_conversation_registry_keeps_listable_rows.py).

WHAT RUNS HERE. ui/FleetConvIdentity.cs's ConvListing (the transcript index, its paging and the
registry read) is compiled with ui/harness/ConvListingHarness.cs and executed. The WPF window
itself cannot be constructed in a test, so the call sites in ui/CopilotChat.cs are pinned as
source shape: no serializer left at the default ceiling, no swallowing catch in the readers of
the list, no startup-only cap.

Skips only on a non-Windows host. On Windows a missing csc skips unless REQUIRE_CSC=1 (set by CI).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

UI = os.path.join(REPO, "ui")
FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")
SOURCES = [os.path.join(UI, "FleetConvIdentity.cs"),
           os.path.join(UI, "harness", "ConvListingHarness.cs")]

CHAT = open(os.path.join(UI, "CopilotChat.cs"), encoding="utf-8-sig").read()

needs_csc = pytest.mark.skipif(os.name != "nt", reason="C# compiled by the .NET Framework csc")


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    if not os.path.isfile(CSC):
        if os.environ.get("REQUIRE_CSC", "").strip() == "1":
            pytest.fail("csc.exe is not at %s and REQUIRE_CSC=1" % CSC)
        pytest.skip("csc.exe is not at %s" % CSC)
    d = tmp_path_factory.mktemp("conv_listing")
    out = str(d / "ConvListingHarness.exe")
    r = childproc.run([CSC, "/nologo", "/target:exe", "/out:" + out,
                       "/r:" + os.path.join(FW, "System.Web.Extensions.dll")] + SOURCES,
                      timeout=300)
    assert r.returncode == 0 and os.path.isfile(out), (r.returncode, r.stdout, r.stderr)
    return out


def _run(exe, *args):
    r = childproc.run([exe] + [str(a) for a in args], timeout=120)
    assert r.returncode == 0, (args, r.returncode, r.stdout, r.stderr)
    return r.stdout


def _pages(exe, d):
    return json.loads(_run(exe, "pages", d))


def _noon(days_ago):
    day = datetime.date.today() - datetime.timedelta(days=days_ago)
    return time.mktime(datetime.datetime(day.year, day.month, day.day, 12, 0, 0).timetuple())


def _make(d, names_and_times):
    os.makedirs(d, exist_ok=True)
    for name, t in names_and_times:
        p = os.path.join(d, name)
        with open(p, "wb") as fh:
            fh.write(b"{}\n")
        os.utime(p, (t, t))


# ---------------------------------------------------------------- paging (executed)

@needs_csc
def test_with_fewer_than_a_page_everything_is_listed_at_once_newest_first(exe, tmp_path):
    """GOLDEN: below the first page the behaviour is what it always was -- one page, all rows."""
    d = str(tmp_path / "t")
    _make(d, [("r%02d_a0_w0.jsonl" % i, _noon(1) + i * 60) for i in range(30)])
    pages = _pages(exe, d)
    assert len(pages) == 1 and len(pages[0]) == 30
    assert pages[0][0] == "r29_a0_w0.jsonl" and pages[0][-1] == "r00_a0_w0.jsonl"


@needs_csc
def test_older_days_come_in_pages_after_the_first_eighty(exe, tmp_path):
    d = str(tmp_path / "t")
    items = [("new%03d.jsonl" % i, _noon(1) + i * 30) for i in range(80)]
    for day in (3, 4, 9):
        items += [("d%d_%d.jsonl.gz" % (day, i), _noon(day) + i * 30) for i in range(5)]
    _make(d, items)
    pages = _pages(exe, d)
    assert [len(p) for p in pages] == [80, 5, 5, 5], [len(p) for p in pages]
    assert all(n.startswith("new") for n in pages[0])
    assert all(n.startswith("d3_") for n in pages[1])      # newest older day first
    assert all(n.startswith("d9_") for n in pages[3])


@needs_csc
def test_all_three_thousand_transcripts_are_reachable_exactly_once(exe, tmp_path):
    """The live count at the time of the report: 3,309 on disk, 80 listed."""
    d = str(tmp_path / "t")
    items = []
    for i in range(3309):
        items.append(("r%05d_a0_w0.jsonl.gz" % i, _noon(1 + i // 90) + (i % 90) * 40))
    _make(d, items)
    pages = _pages(exe, d)
    flat = [n for p in pages for n in p]
    assert len(flat) == 3309 and len(set(flat)) == 3309
    assert len(pages[0]) == 80
    assert max(len(p) for p in pages) <= 300       # no page can stall the UI thread


@needs_csc
def test_a_very_large_day_is_split_not_dropped(exe, tmp_path):
    d = str(tmp_path / "t")
    _make(d, [("big%04d.jsonl" % i, _noon(2) + i) for i in range(700)])
    pages = _pages(exe, d)
    assert sum(len(p) for p in pages) == 700 and max(len(p) for p in pages) <= 300


@needs_csc
def test_sub_agent_transcripts_stay_hidden_and_a_gzip_twin_is_one_row(exe, tmp_path):
    d = str(tmp_path / "t")
    t = _noon(1)
    _make(d, [("a.jsonl", t), ("a.jsonl.gz", t), ("a__sub_research_t1_1.jsonl", t),
              ("b__sub_research_t2_1.jsonl.gz", t), ("b.jsonl.gz", t - 5)])
    flat = [n for p in _pages(exe, d) for n in p]
    assert sorted(flat) == ["a.jsonl", "b.jsonl.gz"], flat


# ---------------------------------------------------------------- registry read (executed)

def _registry_file(tmp_path, rows, prefix=b""):
    p = tmp_path / "conversations.json"
    p.write_bytes(prefix + json.dumps(rows, ensure_ascii=False).encode("utf-8"))
    return str(p)


@needs_csc
def test_a_registry_over_two_megabytes_still_lists(exe, tmp_path):
    """THE SILENT FAILURE. The live file had crossed 2,097,152 characters; the default
    serializer threw and the old bare catch turned that into an empty list."""
    rows = [{"url": "", "title": "t%d" % i, "source": "fleet", "ts": 1.0 + i,
             "transcript": "x%d.jsonl" % i, "goal": "g" * 2000} for i in range(1500)]
    path = _registry_file(tmp_path, rows)
    assert os.path.getsize(path) > 2 * 1024 * 1024
    got = json.loads(_run(exe, "registry", path))
    assert got == {"count": 1500, "error": ""}, got


@needs_csc
def test_a_registry_with_a_bom_reads(exe, tmp_path):
    got = json.loads(_run(exe, "registry", _registry_file(tmp_path, [{"title": "a"}], b"\xef\xbb\xbf")))
    assert got["count"] == 1 and got["error"] == ""


@needs_csc
def test_a_missing_registry_is_empty_without_an_error(exe, tmp_path):
    got = json.loads(_run(exe, "registry", str(tmp_path / "nope.json")))
    assert got == {"count": 0, "error": ""}


@needs_csc
def test_a_corrupt_registry_reports_why_instead_of_an_empty_list(exe, tmp_path):
    p = tmp_path / "conversations.json"
    p.write_text('[{"title": "cut off', encoding="utf-8")
    got = json.loads(_run(exe, "registry", str(p)))
    assert got["count"] == 0 and got["error"], got        # the caller has something to show
    p.write_text('{"not": "an array"}', encoding="utf-8")
    got = json.loads(_run(exe, "registry", str(p)))
    assert got["error"], got


@needs_csc
def test_the_notice_is_one_line_in_both_languages(exe):
    ja = bytes.fromhex(_run(exe, "notice", "ja", "ArgumentException: boom second line").strip()).decode("utf-8")
    en = bytes.fromhex(_run(exe, "notice", "en", "ArgumentException: boom").strip()).decode("utf-8")
    assert ja.startswith("履歴の一部を読めませんでした: ") and "\n" not in ja and "boom" in ja
    assert en.startswith("Part of the history could not be read: ") and "boom" in en


# ---------------------------------------------------------------- the call sites (source shape)

def _body(name, src=CHAT):
    """Text of the first method named `name` in `src`, from its signature to its closing brace."""
    m = re.search(r"^    (?:static |public |private |readonly )*[\w<>\[\],.]+[ \t]+%s\(" % re.escape(name),
                  src, re.M)
    assert m, "%s not found" % name
    i = src.index("{", m.end())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError("unbalanced braces in %s" % name)


#: The readers of the conversation list. A `catch { ... }` with no exception in hand, in any of
#: these, is how three separate failures became "the old chats are just not there".
LIST_READERS = ["ReadConvsRegistry", "TryParseTranscriptLine", "DiscoverTranscripts",
                "LoadTranscriptPage", "LoadOlderPage", "SyncRegistry", "LoadConversations",
                "TranscriptMetaGoal", "RegistryTranscriptLineageFor", "NoteTranscriptLineFailure"]


@pytest.mark.parametrize("name", LIST_READERS)
def test_no_conversation_list_reader_swallows_an_error(name):
    body = _body(name)
    assert not re.search(r"catch\s*\{", body), (
        "%s has a bare catch: an error here must be logged and, for the registry, shown" % name)
    for m in re.finditer(r"catch\s*\(\s*\w+(?:\s+(\w+))?\s*\)\s*\{(.*?)\n?\s*\}", body, re.S):
        var, inner = m.group(1), m.group(2)
        if var is None:
            continue    # a typed catch without a variable is a decision, not a swallow
        assert var in inner, "%s catches %s and never uses it" % (name, var)


def test_the_registry_reader_logs_and_shows_the_failure():
    body = _body("ReadConvsRegistry")
    assert "ConvListing.ReadRegistry" in body and "ConvListing.Diag" in body
    assert "_historyNotice" in body and "UnreadableNotice" in body
    assert "MakeHistoryNotice(_historyNotice)" in _body("RefreshConvList")


def test_an_unreadable_registry_is_never_written_back():
    """Reading nothing and then writing `list + one row` replaces the whole file with that row."""
    for fn in ("RegisterConv", "UnregisterConvs"):
        assert "_lastRegistryError.Length > 0" in _body(fn), fn


def test_every_json_serializer_in_the_ui_lifts_the_two_megabyte_default():
    for name in sorted(os.listdir(UI)):
        if not name.endswith(".cs"):
            continue
        src = open(os.path.join(UI, name), encoding="utf-8-sig").read()
        for m in re.finditer(r"new JavaScriptSerializer\s*(\(\s*\))?\s*(\{[^}]*\})?", src):
            assert m.group(2) and "MaxJsonLength = int.MaxValue" in m.group(2), (
                "%s: a JavaScriptSerializer is left at the 2,097,152-character default "
                "(it fails on a large .fleet file or a long transcript line)" % name)


def test_the_startup_only_cap_is_gone_and_older_rows_are_loadable():
    disc = _body("DiscoverTranscripts") + _body("LoadTranscriptPage")
    assert "budget" not in disc, "the newest-80 cap is back"
    assert "ConvListing.ListMainTranscripts" in disc and "ConvListing.PageEnd" in disc
    refresh = _body("RefreshConvList")
    assert "MakeOlderRow(TranscriptsPending())" in refresh
    assert "LoadOlderPage()" in _body("MakeOlderRow")
    assert 'older_more' in CHAT


def test_opening_an_old_row_still_reads_the_local_transcript_first():
    body = _body("OpenConversation")
    assert body.index("ReadTranscriptLineage(c.Transcripts)") < body.index("OpenFromFleet(url")
    assert "TranscriptMetaGoal(c.Transcript)" in body       # the full goal comes back with it
