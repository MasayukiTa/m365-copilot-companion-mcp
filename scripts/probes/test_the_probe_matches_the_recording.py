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

import ast
import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OBSERVED = os.path.join(HERE, "uploadfile_observed.json")
REPLAY = os.path.join(HERE, "replay_upload_with_our_token.py")
RECORDER = os.path.join(HERE, "observe_real_upload.py")


def _observed():
    return json.loads(io.open(OBSERVED, encoding="utf-8").read())


def _code(path):
    """Source with comments AND docstrings removed, so prose cannot satisfy or trip a check.

    THE THIRD SUBSTRING-VERSUS-PROSE FALSE POSITIVE IN ONE DAY. First a guard forbidding
    `S.get(` fired on `_DRAIN_ATTEMPTS.get(`. Then a check forbidding a reconstructed binary
    part matched the COMMENT explaining that very mistake, so it was narrowed to skip comment
    lines. Then it matched the module DOCSTRING, which also explains the mistake -- because the
    whole point of these files is that they write down what went wrong, and a text search
    cannot tell an explanation from the thing explained.

    Docstrings are removed by parsing rather than by pattern, since the pattern is what keeps
    failing. Other string literals stay: the checks below look for real code such as
    `headers["Authorization"] = ...`, which is a literal in an assignment, not prose."""
    src = io.open(path, encoding="utf-8", errors="replace").read()
    lines = src.splitlines()
    blank = set()
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            body = getattr(node, "body", None) or []
            if not body:
                continue
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                end = getattr(first, "end_lineno", first.lineno)
                blank.update(range(first.lineno, end + 1))
    except SyntaxError:
        pass
    return "\n".join("" if (i + 1) in blank else l
                     for i, l in enumerate(lines) if not l.lstrip().startswith("#"))


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


# ---- the recorder keeps the auth shape and nothing more ------------------------------------

def test_the_recorder_records_the_scheme_word_and_not_the_credential():
    code = _code(RECORDER)
    assert 'split(" ")[0]' in code, "the scheme is no longer taken as the first word only"
    assert "cookie_header_present" in code
    assert "authorization_scheme" in code
