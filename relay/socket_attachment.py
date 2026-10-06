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

THE REQUEST IS CAPTURED, NEVER REBUILT. The page sends six multipart fields -- scenario,
conversationId, FileBase64 as a data URI, and optionsSets THREE TIMES -- and eighteen headers,
among them x-anchormailbox, which routes it. A probe that reconstructed that request failed
three times in one afternoon and returned three 403s that were each reported as an answer about
the credential. relay/chathub.py reached the same conclusion independently on 2026-08-20 for the
socket frame: composed is REJECTED, captured is accepted. So this opens the page, lets it make
its own request, captures the bytes and headers, and re-issues them with our token.

FAILURE IS A REFUSAL, NOT A SILENT FALLBACK TO NO FILE. Every path returns None, and the caller
must treat None as "use a tab". An instruction about a file that was never delivered comes back
as a confident answer about nothing -- which is the failure agent_profiles already names in its
tab path and the one this module must not reintroduce.
"""
from __future__ import annotations

import os

#: Headers a replay must not forward: HTTP/2 framing, a length requests recomputes, the
#: credential we are replacing, and an encoding we do not want applied twice.
_DROP = {":authority", ":method", ":path", ":scheme", "content-length", "authorization",
         "accept-encoding", "host"}

UPLOAD_URL = "https://substrate.office.com/m365Copilot/UploadFile"

#: How long to wait for the page to make its own upload request after the file is set.
CAPTURE_WAIT_S = 25.0


def annotation_for(context, agent_url, upload_path, token, *, log=None):
    """Upload `upload_path` and return the messageAnnotations list, or None.

    `context` is a live Playwright browser context; `token` is the bearer the socket route
    already holds. Returns None for every failure, including an unreadable file -- the caller
    opens a tab instead, which is what happened before this existed.
    """
    say = log or (lambda _m: None)
    try:
        if not upload_path or not os.path.isfile(upload_path):
            say("[socket_attachment] no such file: %s" % upload_path)
            return None
        if not token:
            say("[socket_attachment] no token; cannot upload")
            return None

        captured = {}
        page = context.new_page()
        try:
            def _on_request(req):
                if "uploadfile" not in (req.url or "").lower() or captured:
                    return
                try:
                    captured["body"] = req.post_data_buffer
                    captured["headers"] = {k: v for k, v in req.all_headers().items()
                                           if k.lower() not in _DROP}
                except Exception:
                    pass

            page.on("request", _on_request)
            page.goto(agent_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            from relay.agent_profiles import upload_file

            if not upload_file(page, upload_path):
                say("[socket_attachment] the page did not accept the file")
                return None
            waited = 0.0
            while waited < CAPTURE_WAIT_S and not captured:
                page.wait_for_timeout(500)
                waited += 0.5
        finally:
            try:
                page.close()
            except Exception:
                pass

        if not captured:
            say("[socket_attachment] the page's own upload request was never seen")
            return None

        # IMPORTED HERE, NOT AT THE TOP OF THE FUNCTION. It was above the page block, so on a
        # machine without it every failure in this function came back as ModuleNotFoundError
        # -- including failures that had nothing to do with HTTP. An import belongs where the
        # work it does begins.
        import requests

        headers = dict(captured["headers"])
        headers["Authorization"] = "Bearer " + token
        r = requests.post(UPLOAD_URL, headers=headers, data=captured["body"], timeout=45)
        if r.status_code not in (200, 201):
            say("[socket_attachment] upload refused: HTTP %s" % r.status_code)
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
