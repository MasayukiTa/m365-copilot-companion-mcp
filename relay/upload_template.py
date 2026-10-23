# -*- coding: utf-8 -*-
"""The shape of the page's own UploadFile request, captured once and reused without a page.

WHY THIS EXISTS (2026-10-23). relay/socket_attachment.py used to open the Copilot page for EVERY
attachment, put the file in the page's file input, and copy the request the page then made. The
owner's rule of 2026-10-14 (relay/no_tab.py) made that page a refused tab, so from that day every
attachment ended "CopilotTabForbidden ... attachment upload page" and the image-reading side agent
(ANALYZE) failed on every run. The attachment over the socket is an established feature (measured
2026-09-17/18, see relay/socket_attachment.py) and the rule is about not RUNNING A GOAL in a tab.

WHAT IS DIFFERENT NOW. The page is needed to learn the request's SHAPE (eighteen headers, among
them x-anchormailbox, and six multipart fields), not to send each file. The shape does not depend
on which file is sent, so it is captured once per agent surface, kept in .fleet/upload_templates,
and every later upload is the cached request with one thing replaced: the bytes of the
FileBase64 field. Everything else -- every header, every other field, the boundary -- is the
captured bytes, untouched. A request composed from scratch was refused with 403 three times on
2026-09-18 and "only the captured request passes"; this keeps that property for all but the
payload, which by construction is the only part that differs between two files.

WHY THE CAPTURE PAGE IS ALLOWED. It is the same kind of page as the token light page in
relay/profile_token.py: it loads the signed-in agent surface, sets ONE test image on the file
input (a side effect: that image is uploaded to Copilot once), reads the request the page itself
makes, and closes. No Copilot turn is sent and no goal runs in it. It runs only when no cached
shape exists or the backend refused the cached one (see discard / socket_attachment).

WHAT IS STORED. Headers except the credential (the Authorization header is never read into
the file; the caller's token is supplied per request) and the bytes around the FileBase64 value.
The headers include account-routing values, so the cache lives in the git-ignored .fleet
directory like the request templates, and is never logged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import time

UPLOAD_URL = "https://substrate.office.com/m365Copilot/UploadFile"

#: Headers a replay must not forward: HTTP/2 framing, a length requests recomputes, the
#: credential that is supplied per request, and an encoding we do not want applied twice.
DROP = {":authority", ":method", ":path", ":scheme", "content-length", "authorization",
        "accept-encoding", "host"}

#: The multipart field that carries the file, as the page names it.
FILE_FIELD = "FileBase64"

#: How long to wait for the page to make its own upload request after the test file is set.
CAPTURE_WAIT_S = 25.0

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".fleet", "upload_templates")


class UploadTemplate:
    """Headers plus the bytes before and after the file field's value."""

    def __init__(self, headers, prefix: bytes, suffix: bytes, value_prefix: str,
                 fields=None, saved_at=0.0):
        self.headers = dict(headers)
        self.prefix = prefix
        self.suffix = suffix
        self.value_prefix = value_prefix          # e.g. "data:image/png;base64,"
        self.fields = list(fields or [])          # field names seen, for diagnosis only
        self.saved_at = saved_at


def cache_path(agent_url: str, directory=None) -> str:
    """One file per agent surface: x-gptid and the routing headers belong to the agent."""
    key = hashlib.sha256((agent_url or "").encode("utf-8")).hexdigest()[:16]
    return os.path.join(directory or CACHE_DIR, "upload_%s.json" % key)


def split_multipart(headers, body: bytes):
    """Split a captured body around the FileBase64 value. Raises ValueError if it has none.

    Returns (prefix, suffix, value_prefix, field_names). The value is whatever lies between the
    blank line after the field's part headers and the next boundary line.
    """
    ctype = ""
    for k, v in headers.items():
        if k.lower() == "content-type":
            ctype = v
    m = re.search(r'boundary="?([^";]+)"?', ctype or "")
    if not m:
        raise ValueError("the captured request has no multipart boundary")
    boundary = m.group(1).encode("latin-1")
    names = [n.decode("utf-8", "replace") for n in
             re.findall(rb'Content-Disposition:\s*form-data;\s*name="([^"]+)"', body, re.I)]
    at = re.search(rb'name="' + re.escape(FILE_FIELD.encode()) + rb'"', body)
    if not at:
        raise ValueError("the captured request has no %s field (fields: %s)"
                         % (FILE_FIELD, ", ".join(names) or "none"))
    start = body.find(b"\r\n\r\n", at.end())
    if start < 0:
        raise ValueError("the %s part has no body" % FILE_FIELD)
    start += 4
    end = body.find(b"\r\n--" + boundary, start)
    if end < 0:
        raise ValueError("the %s part is not closed by a boundary" % FILE_FIELD)
    value = body[start:end]
    comma = value.find(b",")
    if not value.startswith(b"data:") or comma < 0 or b"base64" not in value[:comma]:
        raise ValueError("the %s value is not a base64 data URI" % FILE_FIELD)
    return body[:start], body[end:], value[:comma + 1].decode("ascii"), names


#: THE MINIMAL REQUEST, MEASURED 2026-10-23 (no page needed). Dropping the page's headers one at a
#: time with the same image and token: only `content-type` (it carries the boundary) and
#: `x-scenario` were needed (400 / 403 without them); x-anchormailbox, x-gptid, x-variants, origin,
#: referer, user-agent and the sec-* headers were not. A body composed with our own boundary and a
#: fresh random conversationId was accepted (HTTP 200, "Success"). The 403s of 2026-09-18 that this
#: module's history blames on "composed requests" were requests with no x-scenario header.
COMPOSED_SCENARIO_HEADER = "officeweb"
COMPOSED_FIELDS = (("scenario", "UploadImage"),
                   ("conversationId", None),          # a fresh uuid per request
                   ("FileBase64", None),              # the data URI
                   ("optionsSets", "cwcgptvsan"),
                   ("optionsSets", "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch"),
                   ("optionsSets", "gptvnorm2048"))


def data_uri_for(file_path: str, default_prefix: str = "data:image/png;base64,") -> str:
    """A base64 data URI for an image file. Raises ValueError for anything that is not an image
    (a .txt was measured to be refused: HTTP 400 InvalidFileDataUri)."""
    mime = mimetypes.guess_type(file_path)[0] or ""
    if not mime.startswith("image/"):
        raise ValueError("%s is not an image; the image upload endpoint refuses other data"
                         % os.path.basename(file_path))
    with open(file_path, "rb") as fh:
        return "data:%s;base64,%s" % (mime, base64.b64encode(fh.read()).decode("ascii"))


def build_composed(file_path: str):
    """(headers, body) for the minimal request: no page, no captured shape. Authorization is
    added by the caller."""
    import uuid

    boundary = "----upload" + uuid.uuid4().hex
    value = data_uri_for(file_path)
    body = b""
    for name, fixed in COMPOSED_FIELDS:
        if name == "conversationId":
            v = str(uuid.uuid4())
        elif name == "FileBase64":
            v = value
        else:
            v = fixed
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, name, v)).encode("utf-8")
    body += ("--%s--\r\n" % boundary).encode("ascii")
    return ({"content-type": "multipart/form-data; boundary=" + boundary,
             "x-scenario": COMPOSED_SCENARIO_HEADER}, body)


def build(template: UploadTemplate, file_path: str):
    """(headers, body) for `file_path`: the captured request with the file value replaced.

    Nothing else changes -- the captured conversationId included. MEASURED 2026-10-23: a docId
    uploaded under the captured conversationId was read by the model on a socket turn in a
    different conversation (Analyst agent), so the field need not match the socket's.
    Authorization is not set here; the caller adds it.
    """
    with open(file_path, "rb") as fh:
        raw = fh.read()
    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    prefix = template.value_prefix
    if prefix.startswith("data:image/") and not mime.startswith("image/"):
        raise ValueError("%s is not an image but the cached request carries an image upload"
                         % os.path.basename(file_path))
    if mime.startswith("image/"):
        prefix = "data:%s;base64," % mime
    value = prefix.encode("ascii") + base64.b64encode(raw)
    return dict(template.headers), template.prefix + value + template.suffix


def save(template: UploadTemplate, agent_url: str, directory=None) -> bool:
    path = cache_path(agent_url, directory)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ts": time.time(),
                "headers": template.headers,
                "prefix_b64": base64.b64encode(template.prefix).decode("ascii"),
                "suffix_b64": base64.b64encode(template.suffix).decode("ascii"),
                "value_prefix": template.value_prefix, "fields": template.fields,
            }, fh, ensure_ascii=False)
        os.replace(tmp, path)               # a half-written cache must never be readable
        return True
    except Exception:
        return False


def load(agent_url: str, directory=None):
    """The cached request shape for this agent, or None (unreadable is not an error)."""
    try:
        with open(cache_path(agent_url, directory), encoding="utf-8") as fh:
            d = json.load(fh)
        t = UploadTemplate(d["headers"], base64.b64decode(d["prefix_b64"]),
                           base64.b64decode(d["suffix_b64"]), d["value_prefix"],
                           d.get("fields"), float(d.get("ts") or 0))
    except Exception:
        return None
    if any(k.lower() == "authorization" for k in t.headers):
        return None                         # a credential on disk is never reused
    return t


def discard(agent_url: str, directory=None) -> None:
    """Forget the cached shape (the backend refused a request built from it)."""
    try:
        os.remove(cache_path(agent_url, directory))
    except OSError:
        pass


def from_capture(headers, body: bytes) -> UploadTemplate:
    """A template from the page's own request. Raises ValueError if the body is not usable."""
    kept = {k: v for k, v in headers.items() if k.lower() not in DROP}
    prefix, suffix, value_prefix, names = split_multipart(kept, body)
    return UploadTemplate(kept, prefix, suffix, value_prefix, names, time.time())


def make_probe_png(directory: str) -> str:
    """A small PNG carrying random letters. Its only use is to make the page send a request."""
    from PIL import Image, ImageDraw

    word = "".join(random.choice("abcdefghjkmnpqrstuvwxyz") for _ in range(6))
    img = Image.new("RGB", (240, 90), (255, 255, 255))
    ImageDraw.Draw(img).text((12, 36), word, fill=(0, 0, 0))
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "probe_%s.png" % word)
    img.save(path, optimize=True)
    return path


def capture(context, agent_url, *, log=None, directory=None):
    """Open the agent surface once, let the page make its own UploadFile request for a test
    image, derive and cache the shape, close. NO Copilot turn is sent. Returns the template or
    None (with the reason logged).

    This is the only page the attachment path opens; it is declared in
    relay/test_no_goal_path_opens_a_tab.py next to the token light page.
    """
    say = log or (lambda _m: None)
    from relay import blocking_calls
    from relay.profile_token import _claim, _release

    probe = None
    try:
        from relay.temp_home import temp_dir
        probe = make_probe_png(temp_dir("upload_template"))
        captured = {}

        def _on_request(req):
            if "uploadfile" not in (req.url or "").lower() or captured:
                return
            try:
                captured["body"] = req.post_data_buffer
                captured["headers"] = dict(req.all_headers())
            except Exception:
                pass

        with blocking_calls.blocking("upload_template_capture", 150):
            page = context.new_page()
            _claim(page, "upload template")
            try:
                page.on("request", _on_request)
                try:
                    page.goto(agent_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass                      # a slow load is not a failure; the input is awaited
                page.wait_for_timeout(3000)
                from relay.agent_profiles import upload_file
                if not upload_file(page, probe):
                    say("[upload_template] the page did not accept the test image")
                    return None
                waited = 0.0
                while waited < CAPTURE_WAIT_S and not captured:
                    page.wait_for_timeout(500)
                    waited += 0.5
            finally:
                _release(page)
        if not captured.get("body"):
            say("[upload_template] the page's own upload request was never seen")
            return None
        tpl = from_capture(captured["headers"], captured["body"])
        if not save(tpl, agent_url, directory):
            say("[upload_template] captured, but the cache could not be written")
        say("[upload_template] captured the upload shape: %d headers, fields %s"
            % (len(tpl.headers), tpl.fields))
        return tpl
    except Exception as exc:
        say("[upload_template] capture failed: %s: %s" % (type(exc).__name__, str(exc)[:160]))
        return None
    finally:
        try:
            if probe:
                os.remove(probe)
        except OSError:
            pass
