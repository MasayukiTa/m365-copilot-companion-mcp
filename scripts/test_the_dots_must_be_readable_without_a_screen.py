# -*- coding: utf-8 -*-
"""The health dots decide what a person does. They existed only on the screen.

2026-09-17 13:05. Asked why the server dot was lit, this tool could not say. It knew the
server answered on 127.0.0.1:8000 and that the code it was running had fallen behind the
checkout -- it prints both -- but not what the DOT was showing, and the dot is the thing a
person looks at before deciding anything. The answer had to be read off a tooltip by hand and
relayed. That is the exact inversion of how this repository is supposed to work: the command
line first, the GUI after.

It was amber, and amber was right -- "the server is responding but is still running the code
it started on". So the indicator was correct and the instrument around it was missing, which
is the harder of the two to notice.

WHAT THE STRIP MUST CARRY, and why each piece:
  * the STATE as a word, because "red" and "amber" mean different things to a reader and the
    difference is the whole message;
  * the DETAIL, because a colour without a reason sends a person to the source;
  * the AGE of the last check per dot, and the file's own mtime, because a strip that stopped
    being swept is a different failure from a strip that is all green -- and before this
    neither was visible from here.

NOT A SECOND OPINION. status.py keeps computing its own cross-checks. Two views that can
disagree is the point: a disagreement between the panel and the shell is the defect class that
cost a month in September, and it can only be seen when both are written down.
"""
from __future__ import annotations

import io
import json
import re
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import status as ST                        # noqa: E402

STRIP = os.path.join(REPO, ".fleet", "health_strip.json")


class _Rep:
    """The smallest thing section_health_strip will talk to."""

    def __init__(self):
        self.rows = []
        self.sections = []

    def section(self, title):
        self.sections.append(title)

    def row(self, mark, key, detail):
        self.rows.append((mark, key.strip(), detail))


def _run(tmp_path, monkeypatch, payload, age_s=0.0, stale_after_s=90.0):
    p = tmp_path / ".fleet" / "health_strip.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if age_s:
        st = os.stat(str(p))
        os.utime(str(p), (st.st_atime, st.st_mtime - age_s))
    monkeypatch.setattr(ST.os.path, "abspath",
                        lambda _x: str(tmp_path / "scripts" / "status.py"))
    rep = _Rep()
    ST.section_health_strip(rep, stale_after_s=stale_after_s)
    return rep


def _dots(**states):
    return {"ts": 0, "dots": [{"key": k, "state": v, "detail": "d-" + k,
                               "checked_age_s": 0.1} for k, v in states.items()]}


def test_a_red_dot_reads_as_a_problem(tmp_path, monkeypatch):
    rep = _run(tmp_path, monkeypatch, _dots(hs_server="red"))
    marks = [m for m, k, _ in rep.rows if k == "hs_server"]
    assert marks and marks[0] == ST.BAD


def test_amber_and_red_do_not_read_the_same(tmp_path, monkeypatch):
    """The question that started this: the operator said "red", the code said amber for stale
    code, and the two mean different things to whoever reads them."""
    red = _run(tmp_path, monkeypatch, _dots(hs_server="red"))
    amber = _run(tmp_path, monkeypatch, _dots(hs_server="yellow"))
    m_red = [m for m, k, _ in red.rows if k == "hs_server"][0]
    m_amber = [m for m, k, _ in amber.rows if k == "hs_server"][0]
    assert m_red != m_amber, "a stale server and an unreachable one must not print alike"


def test_the_reason_is_printed_not_just_the_colour(tmp_path, monkeypatch):
    rep = _run(tmp_path, monkeypatch, _dots(hs_server="yellow"))
    detail = [d for _m, k, d in rep.rows if k == "hs_server"][0]
    assert "d-hs_server" in detail, "a colour without its reason sends the reader to the source"


def test_a_strip_that_stopped_being_swept_is_a_failure(tmp_path, monkeypatch):
    """ALL GREEN AND NOT UPDATING is the shape this exists to catch. Before the strip was
    written down, a cockpit that had stopped sweeping looked exactly like a healthy one."""
    rep = _run(tmp_path, monkeypatch, _dots(hs_server="green"), age_s=600.0)
    ages = [(m, d) for m, k, d in rep.rows if k == "strip age"]
    assert ages and ages[0][0] == ST.BAD, "a stale strip printed as healthy"
    assert "not sweeping" in ages[0][1]


def test_a_fresh_strip_says_so(tmp_path, monkeypatch):
    rep = _run(tmp_path, monkeypatch, _dots(hs_server="green"), age_s=0.0)
    ages = [(m, d) for m, k, d in rep.rows if k == "strip age"]
    assert ages and ages[0][0] == ST.OK


def test_a_missing_strip_is_unknown_not_healthy(tmp_path, monkeypatch):
    """An older cockpit does not write the file. Absent evidence must not read as good news --
    the rule this repository applies to every other dot."""
    monkeypatch.setattr(ST.os.path, "abspath",
                        lambda _x: str(tmp_path / "scripts" / "status.py"))
    rep = _Rep()
    ST.section_health_strip(rep)
    assert rep.rows and rep.rows[0][0] == ST.UNK
    assert "rebuild" in rep.rows[0][2]


def test_the_cockpit_actually_publishes_it():
    """The other half: a reader with no writer is a reader that always says 'not published'.
    Source-level, because the writer is a C# panel that cannot be imported here."""
    body = io.open(os.path.join(REPO, "ui", "FleetCockpit.cs"),
                   encoding="utf-8-sig", errors="replace").read()
    assert "PublishHealthStrip" in body
    assert "health_strip.json" in body
    assert "PublishHealthStrip();" in body, "defined but never called from the sweep"


def _cockpit_source():
    return io.open(os.path.join(REPO, "ui", "FleetCockpit.cs"),
                   encoding="utf-8-sig", errors="replace").read()


def _declared_dot_count(body):
    m = re.search(r"const int HEALTH_DOT_COUNT = (\d+);", body)
    assert m, "HEALTH_DOT_COUNT is gone; this test no longer knows what to expect"
    return int(m.group(1))


def _declared_keys(body):
    m = re.search(r"_healthKeys = \{([^}]*)\}", body)
    assert m, "_healthKeys is gone"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_the_count_and_the_names_agree_in_the_source():
    """THE SIZES MUST COME FROM ONE PLACE. Adding a seventh dot compiled cleanly and threw
    IndexOutOfRange on the first poll, because the DotState array was six `new DotState()` in a
    row while the count said seven -- and the whole window died at startup, so the symptom was
    an exit code and an empty desktop. Nothing in the build could have caught it; this can."""
    body = _cockpit_source()
    n = _declared_dot_count(body)
    keys = _declared_keys(body)
    assert len(keys) == n, "HEALTH_DOT_COUNT is %d but %d names are published" % (n, len(keys))
    assert len(set(keys)) == len(keys), "two dots share a name: %r" % keys
    # And the array is BUILT to the count rather than typed out, which is what stops the next
    # dot from doing this again.
    assert "new DotState[HEALTH_DOT_COUNT]" in body, \
        "the dot-state array is back to a hand-written list of literals"


def test_the_fix_pill_does_not_offer_to_fix_the_frozen_set():
    """RunFix restarts processes. A frozen set that differs is waiting on a person, and no
    restart changes it -- so when this dot went amber the pill appeared and its button did
    nothing at all (RunFix reads dots 0..5, so the target mask came out 0). An offer to fix
    that fixes nothing spends the one action a person trusts."""
    body = _cockpit_source()
    assert 'if (_healthKeys[i] != "hs_frozen"' in body, \
        "the frozen dot is raising the Fix pill again"


@pytest.mark.skipif(not os.path.isfile(STRIP), reason="no cockpit has published a strip here")
def test_the_published_strip_parses_and_names_every_dot():
    """Against the real file when one exists: the format the reader assumes is the format the
    panel writes.

    THE EXPECTED COUNT IS READ FROM THE PANEL, not typed here. It was `== 6`, and a seventh dot
    turned this into a failing test about a change that was correct -- a guard that has to be
    edited every time the thing it guards grows teaches people to edit it without looking."""
    d = json.loads(io.open(STRIP, encoding="utf-8").read())
    dots = d.get("dots") or []
    body = _cockpit_source()
    want = _declared_dot_count(body)
    assert len(dots) == want, "the panel declares %d dots; %d were published" % (want, len(dots))
    assert [r.get("key") for r in dots] == _declared_keys(body), \
        "the published names are not the panel's own list, in its order"
    for row in dots:
        assert row.get("key") and row.get("state")
        assert row["state"] in ("green", "yellow", "red", "gray", "checking"), row["state"]
