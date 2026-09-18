# -*- coding: utf-8 -*-
"""The probe's request must be the one that was observed, not the one someone remembered.

WHAT HAPPENED. A CDP recording on 2026-09-17 captured the page uploading an image:

    POST https://substrate.office.com/m365Copilot/UploadFile
      scenario       = "UploadImage"
      conversationId = a real conversation the page was in
      FileBase64     = "data:image/png;base64,..."   -- a TEXT field holding a data URI

The field list was never written down. On 2026-09-18 a probe was built from recollection,
sent the bytes as a BINARY multipart part named "file" with an invented conversationId, got
403, and the 403 was reported as "the audience we hold does not cover this path". It was
nothing of the kind: a refusal of a request nobody makes says nothing about the request
everybody makes. The evidence that would have shown this immediately was in the recording the
whole time.

So the observation is data now -- uploadfile_observed.json -- and the probe builds from it.
This is what keeps them married: the check is not "did someone remember to update both", it is
a test that fails the moment they disagree.

NO NETWORK, NO BROWSER, NO ENDPOINT. This reads two files.
"""
from __future__ import annotations

import io
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
OBSERVED = os.path.join(HERE, "uploadfile_observed.json")
PROBE = os.path.join(HERE, "can_we_upload.py")


def _observed():
    return json.loads(io.open(OBSERVED, encoding="utf-8").read())


def _probe_src():
    return io.open(PROBE, encoding="utf-8", errors="replace").read()


def _probe_code():
    """The probe's source with comment lines removed.

    A check written against the whole file matched the COMMENT that explains the old broken
    shape -- the second substring-vs-prose false positive in one day, after a guard forbidding
    `S.get(` fired on `_DRAIN_ATTEMPTS.get(`. A comment describing a mistake must not be
    indistinguishable from the mistake, or the only way to keep the check green is to stop
    writing down what went wrong.
    """
    out = []
    for line in _probe_src().splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_the_recording_still_names_the_three_fields():
    """If this file is ever edited down, everything below stops meaning anything."""
    names = [f["name"] for f in _observed()["fields"]]
    assert names == ["scenario", "conversationId", "FileBase64"], names


def test_the_probe_sends_exactly_the_observed_fields():
    """THE DEFECT, pinned from the source: the probe named a field the page never sends."""
    code = _probe_code()
    for name in [f["name"] for f in _observed()["fields"]]:
        assert '"%s"' % name in code, "the probe does not send %r" % name
    assert 'files={"file"' not in code and "files=files" not in code, \
        "the probe is still sending a binary file part; the page sends FileBase64 as text"


def test_the_bytes_go_as_a_data_uri_not_as_a_part():
    """The encoding is half the shape. base64 alone is not what the page sends -- the field
    carries the full `data:image/png;base64,` prefix."""
    src = _probe_src()
    assert "data:image/png;base64," in src, \
        "the probe does not build the data URI prefix the recording shows"
    obs = _observed()
    fb = [f for f in obs["fields"] if f["name"] == "FileBase64"][0]
    assert fb["observed_value_prefix"].startswith("data:image/png;base64,")
    assert fb["kind"] == "text", "the recording says text; a change here changes the probe"


def test_the_probe_refuses_rather_than_guesses_when_they_disagree():
    """A mismatch must stop the run, not be sent anyway and then interpreted. Both directions:
    a field the recording names and the probe omits, and one the probe invents."""
    src = _probe_src()
    assert "REFUSING: the recording names fields this probe does not send" in src
    assert "REFUSING: this probe sends fields the recording does not" in src


def test_the_probe_prefers_a_real_conversation_and_says_when_it_could_not():
    """An invented conversationId is a second changed variable. The first version called it
    harmless on the assumption that the endpoint validates the body before the conversation,
    which nothing established."""
    src = _probe_src()
    assert "_live_conversation_id" in src
    assert "INVENTED -- this is a second changed variable" in src


def test_no_reading_claims_the_credential_from_a_bare_refusal():
    """The old reading said a 401/403 meant the audience does not cover this path. It only
    narrows the credential if the other variables matched, and on the run that produced it they
    did not."""
    src = _probe_src()
    assert "the audience we hold does not open this path" not in src, \
        "the retracted reading is back in the probe"
    assert re.search(r"narrows things ONLY if", src), \
        "the refusal branch no longer states what it depends on"


def test_the_recording_records_what_it_did_not_capture():
    """Two things were never seen -- which credential the page used, and the response. An
    observation file that lists only what it saw invites the gap to be filled in by assumption,
    which is exactly how the 403 got over-read."""
    obs = _observed()
    missing = " ".join(obs["what_was_not_recorded"]).lower()
    assert "authorization" in missing
    assert "response" in missing
