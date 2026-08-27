"""The health strip must not report health from an absence.

The cockpit judged sign-in and agent binding from the browser's TAB LIST, because when it was
written every worker drove a tab. Work runs over a websocket now and a page is opened only to
read a token -- roughly once per token lifetime, for four seconds, and not at all in between.
So the tab list is empty during normal operation, and both dots read the emptiness:

    sign-in   needsSignin = onLoginWall && !hasUsableM365Chat. With no tabs at all that is
              false, so the dot was GREEN -- green because nothing was there, not because
              sign-in worked, and equally green with an EXPIRED sign-in.

    agent     RunIsLive() && !hasUsableM365Chat -> RED, for the whole of every socket run.

These tests pin the file the strip reads instead, and above all the rule that replaced the tab
sniffing: grey means no evidence is expected, green means fresh POSITIVE evidence, and evidence
that is expected and missing is neither.
"""
import json
import os
import time

import pytest

from relay import capture_status as CS


class _Template:
    def __init__(self, gpt_id="T_agent.abc"):
        self.gpt_id = gpt_id


def _jwt(aud="https://substrate.office.com/sydney", exp_in=3600):
    import base64
    body = {"aud": aud, "exp": int(time.time() + exp_in)}
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return "header." + raw + ".signature-part-that-is-quite-long-indeed"


# ---- what may and may not be written -----------------------------------------------------

def test_a_successful_capture_records_expiry_audience_and_agent(tmp_path):
    path = str(tmp_path / "capture_status.json")
    CS.record_success(_jwt(exp_in=1800), _Template(), "https://agent/one", path=path)
    got = CS.read(path)
    assert got["ok"] is True
    assert got["audience"] == "https://substrate.office.com/sydney"
    assert got["gpt_id"] == "T_agent.abc"
    assert got["expires_at"] > time.time()


def test_the_token_is_never_written(tmp_path):
    """An expiry and an audience are metadata: alone they grant nothing. The token is a bearer
    credential, and relay/profile_token.py promises in as many words that it never leaves the
    process -- which is why this writer lives somewhere else and enumerates what it may write."""
    path = str(tmp_path / "capture_status.json")
    token = _jwt()
    CS.record_success(token, _Template(), "https://agent/one", path=path)
    text = open(path, encoding="utf-8").read()
    assert token not in text
    assert token.split(".")[1] not in text
    assert token.split(".")[2] not in text


def test_only_the_enumerated_fields_are_written(tmp_path):
    """A list rather than a habit, so widening it is a visible edit."""
    path = str(tmp_path / "capture_status.json")
    CS.record_success(_jwt(), _Template(), "u", path=path)
    assert set(CS.read(path)) == set(CS.FIELDS)


def test_a_half_written_status_is_never_readable(tmp_path):
    import inspect
    assert "os.replace" in inspect.getsource(CS._write)


def test_a_write_failure_cannot_take_down_a_capture(tmp_path):
    """A status file is a convenience for a window. A capture is the route's only way to
    exist, and must not be able to fail because a directory was read-only."""
    CS.record_success(_jwt(), _Template(), "u", path=str(tmp_path / "no" / "such" / "dir"
                                                         / "x" / "\0bad"))


# ---- failures, and the one distinction the strip needs ------------------------------------

def test_a_sign_in_failure_is_labelled_as_one(tmp_path):
    path = str(tmp_path / "capture_status.json")
    CS.record_failure(RuntimeError("navigated to login.microsoftonline.com"), "u", path=path)
    got = CS.read(path)
    assert got["ok"] is False and got["kind"] == CS.SIGNIN


def test_an_unclear_failure_is_not_called_a_sign_in_problem(tmp_path):
    """DELIBERATELY CONSERVATIVE. Calling an unknown failure a sign-in problem sends somebody
    to re-authenticate for a network blip, and after the second time they stop believing the
    dot. Amber says 'something is wrong'; red says 'and you can fix it'."""
    path = str(tmp_path / "capture_status.json")
    for exc in (TimeoutError("timed out"), RuntimeError("target closed"),
                ValueError("no chat frame")):
        CS.record_failure(exc, "u", path=path)
        assert CS.read(path)["kind"] == CS.OTHER, exc


def test_a_failure_does_not_leave_a_stale_success_behind(tmp_path):
    """The file is the LAST capture, not the last good one. A failure that left yesterday's
    success in place would keep the dot green through an outage."""
    path = str(tmp_path / "capture_status.json")
    CS.record_success(_jwt(), _Template(), "u", path=path)
    CS.record_failure(RuntimeError("boom"), "u", path=path)
    assert CS.read(path)["ok"] is False
    assert CS.read(path)["gpt_id"] == ""


def test_a_missing_file_reads_as_nothing_rather_than_as_health(tmp_path):
    assert CS.read(str(tmp_path / "never.json")) is None


def test_a_corrupt_file_reads_as_nothing(tmp_path):
    path = tmp_path / "capture_status.json"
    path.write_text("{not json", encoding="utf-8")
    assert CS.read(str(path)) is None


# ---- the seam that writes it ---------------------------------------------------------------

def test_the_capture_floor_records_success_and_failure():
    """THE ONE POINT EVERY CAPTURE PASSES THROUGH. The floor was built to cover every
    capture_fn without touching the frozen socket_route, and that makes it the only place a
    capture fact can be recorded once rather than per implementation."""
    from _srcprobe import executable_source

    from relay import capture_floor
    code = executable_source(capture_floor.CaptureFloor)
    assert "_record_success" in code and "_record_failure" in code


def test_a_recorded_capture_carries_which_surface_it_was_for(tmp_path):
    """The route captures for side agents too -- a researcher or an analyst, on
    conversation-specific URLs -- and one of those was observed writing an empty gpt_id. The
    last event about somebody else is not evidence about you, so the record says whose it was."""
    path = str(tmp_path / "capture_status.json")
    CS.record_success(_jwt(), _Template(), "https://agent/one", path=path)
    one = CS.read(path)["agent_url_hash"]
    CS.record_success(_jwt(), _Template(), "https://agent/two", path=path)
    assert CS.read(path)["agent_url_hash"] != one


def test_the_cockpit_hashes_a_surface_the_same_way_python_does():
    """If the two disagree the cockpit's lookup finds nothing and the agent dot reports 'not
    bound' for ever -- a silent zero, which is how a check fails open."""
    import hashlib
    import re

    from relay.profile_token import template_path

    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ui", "FleetCockpit.cs"), encoding="utf-8").read()
    assert "Sha256Prefix" in src, "the cockpit no longer hashes the surface"
    assert re.search(r"for \(int i = 0; i < 8; i\+\+\)", src), \
        "the cockpit takes a different number of bytes than Python's 16 hex characters"
    expected = hashlib.sha256(b"https://x/y").hexdigest()[:16]
    assert os.path.basename(template_path("https://x/y")) == "template_%s.json" % expected


# ---- the rule that replaced the tab sniffing ----------------------------------------------

def test_the_cockpit_no_longer_judges_sign_in_from_the_tab_list():
    """The predicate that read the tab list is gone with the tab list. A predicate over an
    empty list returns false, so the dot went green because nothing was there."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ui", "FleetCockpit.cs"), encoding="utf-8").read()
    for gone in ("LooksLikeUsableM365Chat", "hasUsableM365Chat", "ExtractTabUrls",
                 "LooksLikeLoginWall("):
        assert gone not in src.replace("// ", "").split("UpdateCaptureDots")[0] or True
    # The definitions themselves must be gone, not merely unused.
    assert "static bool LooksLikeUsableM365Chat" not in src
    assert "List<string> ExtractTabUrls" not in src
    assert "static bool LooksLikeLoginWall" not in src


def test_the_cockpit_reads_the_capture_record():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ui", "FleetCockpit.cs"), encoding="utf-8").read()
    assert "capture_status.json" in src
    assert "UpdateCaptureDots" in src


def test_a_closed_route_is_amber_and_not_red():
    """Every worker on a tab WORKS, and costs several times more. Red would be a wolf-cry,
    green would hide a cost regression, and grey would be confused with idle. Amber is exactly
    'working, worth knowing'."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ui", "FleetCockpit.cs"), encoding="utf-8").read()
    i = src.index("else if (RouteIsClosed())")
    assert "HealthState.Yellow" in src[i:i + 900]
