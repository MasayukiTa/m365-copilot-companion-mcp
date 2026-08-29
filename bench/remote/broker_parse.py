"""Validate one broker request. Reads JSON on stdin, prints shell assignments on stdout.

SEPARATE FILE ON PURPOSE. The first version inlined this as `python3 - <<'PY'` inside the
broker, which makes the heredoc stdin -- so the request piped in was discarded and the parser
read its own source as the request. It would have failed closed, but always, and the reason
would have been invisible. A parser that cannot receive its input is not a parser.

Everything here answers one question: is this field allowed to reach a shell?
"""
import base64
import json
import posixpath
import re
import shlex
import sys

INSTANCE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
IMAGE = re.compile(r"^jefzda/sweap-images:[A-Za-z0-9._-]{1,250}$")
VERBS = {"ping", "create", "exec", "put", "get", "destroy", "list"}


def die(msg):
    print("BROKER_ERR=%s" % shlex.quote(msg))
    sys.exit(0)


def main():
    raw = sys.stdin.read()
    try:
        req = json.loads(raw)
    except Exception as e:
        die("request is not JSON: %s" % type(e).__name__)
    if not isinstance(req, dict):
        die("request is not an object")

    verb = req.get("verb")
    if verb not in VERBS:
        die("unknown verb")
    out = {"VERB": verb}

    if verb in ("create", "exec", "put", "get", "destroy"):
        inst = req.get("instance") or ""
        if not isinstance(inst, str) or not INSTANCE.match(inst):
            die("instance id rejected")
        out["INSTANCE"] = inst

    if verb == "create":
        img = req.get("image") or ""
        if not isinstance(img, str) or not IMAGE.match(img):
            die("image not on the allowed repository")
        out["IMAGE"] = img

    if verb == "exec":
        try:
            cmd = base64.b64decode(req.get("cmd") or "", validate=True).decode("utf-8")
        except Exception:
            die("cmd must be base64 utf-8")
        if not cmd.strip():
            die("empty cmd")
        out["CMD_B64"] = base64.b64encode(cmd.encode()).decode()
        t = req.get("timeout", 600)
        if not isinstance(t, int) or isinstance(t, bool) or not (1 <= t <= 3600):
            die("timeout out of range")
        out["TIMEOUT"] = str(t)

    if verb in ("put", "get"):
        # THE ONE FIELD THAT MUST NOT BE TRUSTED AT ALL. Relative, no traversal, and
        # normalised BEFORE it is judged -- "a/../../etc/passwd" is not relative merely
        # because it does not begin with a slash.
        p = req.get("path")
        if not isinstance(p, str) or not p or p.startswith("/") or "\\" in p or "\x00" in p:
            die("path must be relative")
        norm = posixpath.normpath(p)
        if norm == ".." or norm.startswith("../") or norm.startswith("/") or norm == ".":
            die("path escapes the work root")
        out["RPATH"] = norm

    if verb == "put":
        c = req.get("content_b64")
        if not isinstance(c, str):
            die("content_b64 required")
        try:
            base64.b64decode(c, validate=True)
        except Exception:
            die("content_b64 is not base64")
        out["CONTENT_B64"] = c

    for k, v in out.items():
        print("%s=%s" % (k, shlex.quote(v)))


if __name__ == "__main__":
    main()
