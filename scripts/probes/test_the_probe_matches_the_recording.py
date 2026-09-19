# -*- coding: utf-8 -*-
"""The observation must be complete, and the probe must not reconstruct it.

THE SEQUENCE THIS COMES FROM, because the shape of the mistake is the reason the file exists.

A CDP recording on 2026-09-17 captured the page uploading an image. The field list was never
written down. On 2026-09-18 a probe was built from recollection, sent the bytes as a BINARY
multipart part named "file" with an invented conversationId, got 403, and the 403 was reported
as "the audience we hold does not cover this path".

Corrected once: the page sends `FileBase64`, a TEXT field holding a `data:image/png;base64,...`
URI. The probe was fixed, re-run with a real conversationId -- and got 403 again.

Corrected twice, and this is the part that matters: the observation ITSELF was incomplete. It
had been transcribed from a 400-character truncated body_head and listed three fields. The real
body has SIX: scenario, conversationId, FileBase64, and optionsSets THREE TIMES. The probe was
also sending one header where the page sends eighteen, among them x-anchormailbox, which routes
the request. A test had already been written to pin agreement with the three-field version -- a
guard holding a probe to an incomplete record, which would have kept producing confident wrong
answers.

The answer, once the page's own request was replayed with ONLY the Authorization swapped: 200,
result.value "Success". The token was never the problem. Seventeen missing headers were.

So: reconstructing the request is the thing that failed, three times. replay_upload_with_our_
token.py does not reconstruct it -- it captures the page's request and re-issues it. This file
pins that, and pins the observation against being quietly trimmed again.

NO NETWORK, NO BROWSER, NO ENDPOINT. This reads files.
"""
from __future__ import annotations

import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OBSERVED = os.path.join(HERE, "uploadfile_observed.json")
REPLAY = os.path.join(HERE, "replay_upload_with_our_token.py")
RECORDER = os.path.join(HERE, "observe_real_upload.py")
ANALYZE = os.path.join(HERE, "analyze_over_socket.py")


def _observed():
    return json.loads(io.open(OBSERVED, encoding="utf-8").read())


def _code(path):
    """Source with comments AND docstrings removed, so prose cannot satisfy or trip a check.

    THE IMPLEMENTATION MOVED TO conftest.code_only. It lived here as a private helper after
    the third substring-versus-prose false positive in one day, and a FOURTH then happened in a
    check written afterwards, in another file, because a discipline that has to be re-derived
    per file gets re-derived wrongly. One implementation, imported.
    """
    from conftest import code_only

    return code_only(path)


# ---- the observation is complete ----------------------------------------------------------

def test_the_record_names_every_field_the_page_sends():
    """THE SECOND CORRECTION. Three fields was a truncation artefact, not the request."""
    obs = _observed()
    names = [f["name"] for f in obs["fields"]]
    assert names == ["scenario", "conversationId", "FileBase64", "optionsSets"], names
    opts = [f for f in obs["fields"] if f["name"] == "optionsSets"][0]
    assert opts.get("repeated") == 3, \
        "optionsSets appears three times in one body; a record saying otherwise is the old bug"


def test_the_record_keeps_the_header_list_that_was_missing():
    """The probe sent one header where the page sends eighteen. x-anchormailbox routes the
    request, and origin/referer are checked by many Microsoft endpoints."""
    obs = _observed()
    names = obs["request_header_names"]
    for needed in ("authorization", "origin", "referer", "x-anchormailbox", "x-gptid",
                   "x-scenario", "x-variants"):
        assert needed in names, "%s is missing from the recorded header list" % needed


def test_the_record_says_it_supersedes_the_truncated_one():
    """A corrected record that does not say what it corrected invites the same transcription."""
    obs = _observed()
    assert "truncated" in obs["_supersedes"].lower()


def test_the_auth_shape_is_recorded_as_measured():
    """Bearer, no cookie -- measured, after a 403 had already been interpreted as an audience
    answer without it."""
    auth = _observed()["auth"]
    assert auth["authorization_present"] is True
    assert auth["authorization_scheme"] == "Bearer"
    assert auth["cookie_header_present"] is False


def test_the_response_is_recorded_now_that_it_has_been_seen():
    """Zero response bodies had ever been captured, and 'the upload succeeded' was an inference
    from a POST plus a later annotation."""
    resp = _observed()["response"]
    assert resp["status"] == 200
    assert resp["body_shape"]["result"]["value"] == "Success"
    assert "docId" in resp["body_shape"]


def test_the_docid_provenance_is_recorded_rather_than_assumed():
    obs = _observed()
    doc_id = obs["docid_is_the_annotation_id"]["response_docId"]
    assert doc_id.startswith("0-ejp-d1-"), doc_id
    # THE SHAPE, NOT THE VALUE. The hex was a real id for a probe image -- not a credential and
    # not personal data, and still nothing a public repository needs verbatim when the prefix
    # is the whole point.
    assert "<32 hex>" in doc_id, "a real document id is back in the record"


# ---- the probe replays rather than reconstructs --------------------------------------------

def test_the_probe_does_not_rebuild_the_body():
    """THE LESSON. Three attempts reconstructed the request and all three were wrong in a new
    way. The probe must send the page's own bytes."""
    code = _code(REPLAY)
    assert "post_data_buffer" in code, "the probe is not taking the page's own body"
    assert "data=captured[" in code, "the probe is not sending the captured body"
    assert 'files={"file"' not in code, "a reconstructed binary part is back"
    assert "FileBase64" not in code, \
        "the probe is building fields again instead of replaying them"


def test_only_the_authorization_is_replaced():
    """One variable. Everything else is the page's."""
    code = _code(REPLAY)
    assert 'headers["Authorization"] = "Bearer " + token' in code
    for dropped in (":authority", "content-length", "authorization"):
        assert dropped in code, "%s is no longer excluded from the forwarded headers" % dropped


def test_the_pages_own_credential_is_never_forwarded_or_written():
    """It is replaced before the request is built, and the forwarded headers are not stored --
    several of them identify the account."""
    code = _code(REPLAY)
    assert '"forwarded_header_names": forwarded' in code, \
        "the probe should record header NAMES, not header values"
    assert "captured[\"headers\"]" in code
    assert '"headers": captured' not in code, "the probe is writing header values to disk"


def test_the_reading_for_a_refusal_no_longer_overclaims():
    """The retracted reading said a 403 meant the audience does not cover this path. It only
    means that when the rest of the request is the page's own."""
    code = _code(REPLAY)
    assert "it is about the credential" in code
    assert "the audience we hold does not open this path" not in code


def test_a_success_does_not_claim_the_tab_can_be_dropped():
    """200 answers one question. The id's lifetime and whether a socket-sent annotation is
    accepted are separate, and were not asked."""
    code = _code(REPLAY)
    assert "this probe did not ask them" in code


# ---- the ANALYZE probe reads the flag that survives the session --------------------------

def test_the_analyze_probe_does_not_read_a_flag_that_close_clears():
    """IT SCORED A PASS AS A FAILURE ONCE. `_finish()` calls `close()`, which sets
    `socket = False` and drops the page -- so after the loop a finished socket run and a
    finished tab run are indistinguishable. The probe read those and reported that the socket
    wiring had not taken effect, in a run whose own log showed the upload succeeding and whose
    report contained the phrase that exists only in the image.

    `transport` is set when the turn goes out and is never cleared. This pins the reading, not
    the plumbing: relay/test_agent_profiles_research_reason.py pins that the field survives."""
    code = _code(ANALYZE)
    assert 'getattr(s, "transport", "")' in code, "the probe is not reading transport"
    assert "s.socket" not in code, "the probe is back to reading a flag close() clears"
    assert "s.page is not None" not in code


# ---- the recorder keeps the auth shape and nothing more ------------------------------------

def test_the_recorder_records_the_scheme_word_and_not_the_credential():
    code = _code(RECORDER)
    assert 'split(" ")[0]' in code, "the scheme is no longer taken as the first word only"
    assert "cookie_header_present" in code
    assert "authorization_scheme" in code
