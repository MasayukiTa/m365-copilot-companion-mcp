# -*- coding: utf-8 -*-
"""Put a local file on the socket: upload it, and hand back the annotation a frame carries.

WHY THIS EXISTS. Until 2026-09-18 an attachment forced a tab, in two places that did not know
about each other: relay/transport_policy's ATTACHMENT rule, and a guard inside
relay/agent_profiles that returned False from the socket attempt whenever `upload_path` was set.
Retiring the first changed nothing, because the second is what the fleet actually consults --
a policy module can only decide what somebody asks it.

WHAT MADE IT POSSIBLE, all measured rather than reasoned:

    the protocol has a place for it   2026-09-17: the page's own frame carried
                                      messageAnnotations and the server echoed it back with
                                      messageAnnotationSource "UserAnnotated"
    our credential opens UploadFile   2026-09-18: HTTP 200, result.value "Success", by
                                      replaying the page's own request with ONLY the
                                      Authorization header swapped
    a model on the socket SEES it     2026-09-18: an image carrying a randomly generated
                                      phrase was uploaded, its docId sent as an annotation on
                                      a socket turn, and the reply read the phrase back

THE REQUEST SHAPE IS CAPTURED, NEVER COMPOSED. The page sends six multipart fields -- scenario,
conversationId, FileBase64 as a data URI, and optionsSets THREE TIMES -- and eighteen headers,
among them x-anchormailbox, which routes it. A probe that reconstructed that request failed
three times in one afternoon and returned three 403s that were each reported as an answer about
the credential. relay/chathub.py reached the same conclusion independently on 2026-08-20 for the
socket frame: composed is REJECTED, captured is accepted.

UPDATE 2026-10-23, MEASURED: THE PAGE IS NOT NEEDED AT ALL. Dropping the page's headers one at a
time showed that only content-type (the multipart boundary) and x-scenario are required besides
the bearer token; a body composed with our own boundary and a random conversationId got HTTP 200.
The 403s recorded above came from requests that lacked x-scenario, not from "composition". So the
first attempt is that minimal request (relay/upload_template.build_composed): no page, no cache,
a few MB of Python memory. The captured-shape machinery below is the FALLBACK if the endpoint
starts refusing the minimal request.

HOW THE FALLBACK WORKS. The page that makes the request is a browser tab on the Copilot
surface, which the owner's rule (relay/no_tab.py) refuses on a goal's path; for a month that made
every attachment fail ("CopilotTabForbidden ... attachment upload page"). The shape does not
depend on the file, so relay/upload_template.py captures it ONCE per agent surface (a page that
loads, sets a test image, reads the request and closes -- no turn, like the token light page),
caches it, and each upload is that cached request with only the FileBase64 value replaced. A
refusal that says the shape is stale (400/403/404/415/422) discards the cache and captures once
more; a 401 is about the credential and is not.

FAILURE IS A REFUSAL, NOT A SILENT FALLBACK TO NO FILE. Every path returns None, and the caller
ends the goal with the reason (relay/agent_profiles._try_socket). An instruction about a file
that was never delivered comes back as a confident answer about nothing.
"""
from __future__ import annotations

import os

from relay import upload_template as UT

#: Headers a replay must not forward (kept under this name for the tests that pin it).
_DROP = UT.DROP

UPLOAD_URL = UT.UPLOAD_URL

#: Statuses that say the cached REQUEST SHAPE is no longer accepted (as opposed to the credential
#: -- 401 -- or a fault on their side, 429/5xx).
STALE_SHAPE_STATUSES = (400, 403, 404, 415, 422)


def _post(captured_headers, body, token):
    # IMPORTED HERE, NOT AT THE TOP OF THE MODULE. On a machine without it every failure in this
    # function used to come back as ModuleNotFoundError -- including failures that had nothing
    # to do with HTTP. An import belongs where the work it does begins.
    import requests

    headers = dict(captured_headers)
    headers["Authorization"] = "Bearer " + token
    return requests.post(UPLOAD_URL, headers=headers, data=body, timeout=45)


def annotation_for(context, agent_url, upload_path, token, *, log=None):
    """Upload `upload_path` and return the messageAnnotations list, or None.

    `context` is a live Playwright browser context, used ONLY to capture the request shape when
    none is cached; `token` is the bearer the socket route already holds. Returns None for every
    failure, including an unreadable file, and the caller ends the goal with the reason.
    """
    say = log or (lambda _m: None)
    try:
        if not upload_path or not os.path.isfile(upload_path):
            say("[socket_attachment] no such file: %s" % upload_path)
            return None
        if not token:
            say("[socket_attachment] no token; cannot upload")
            return None

        # 1. THE MINIMAL REQUEST, no page and no cache (relay/upload_template.COMPOSED_*).
        headers, body = UT.build_composed(upload_path)
        r = _post(headers, body, token)
        if r.status_code in STALE_SHAPE_STATUSES:
            say("[socket_attachment] the minimal upload request was refused (HTTP %s); "
                "falling back to the captured page shape" % r.status_code)
            r = None
        # 2./3. the page's captured shape, cached; captured once when absent.
        tpl = UT.load(agent_url) if r is None else None
        fresh = False
        if r is None and tpl is None:
            say("[socket_attachment] no cached upload shape for this agent; capturing one")
            tpl = UT.capture(context, agent_url, log=say)
            fresh = True
            if tpl is None:
                return None

        if r is None:
            headers, body = UT.build(tpl, upload_path)
            r = _post(headers, body, token)
        if r.status_code in STALE_SHAPE_STATUSES and not fresh and tpl is not None:
            say("[socket_attachment] the cached upload shape was refused (HTTP %s); "
                "capturing a fresh one" % r.status_code)
            UT.discard(agent_url)
            tpl = UT.capture(context, agent_url, log=say)
            if tpl is None:
                return None
            headers, body = UT.build(tpl, upload_path)
            r = _post(headers, body, token)
        if r.status_code not in (200, 201):
            say("[socket_attachment] upload refused: HTTP %s" % r.status_code)
            if r.status_code in STALE_SHAPE_STATUSES:
                UT.discard(agent_url)       # even a fresh capture was refused: do not keep it
            return None
        body = r.json()
        doc_id = body.get("docId")
        if not doc_id:
            say("[socket_attachment] upload returned no docId")
            return None

        name = body.get("fileName") or os.path.basename(upload_path)
        ext = (os.path.splitext(name)[1] or ".png").lstrip(".").lower()
        say("[socket_attachment] uploaded %s -> %s" % (os.path.basename(upload_path), doc_id))
        # SHAPE TRANSCRIBED FROM THE CAPTURE, not composed. See the module docstring.
        return [{
            "id": doc_id,
            "messageAnnotationMetadata": {"@type": "File", "annotationType": "File",
                                          "fileType": ext, "fileName": name},
            "messageAnnotationType": "ImageFile",
        }]
    except Exception as exc:
        say("[socket_attachment] %s: %s" % (type(exc).__name__, str(exc)[:160]))
        return None

