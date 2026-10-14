# -*- coding: utf-8 -*-
"""Redaction rules for everything the trace viewer prints.

The ledger already refuses to store secret *values* for arguments it recognises by name, but
the viewer must not rely on that: a trace is a document people paste into chats and tickets,
and the ledger keeps up to 2000 characters of every argument and result. So every string that
reaches an output goes through this module, whatever the source field.

Rules (all are deliberately conservative -- when in doubt, show less):

* An argument whose NAME looks like a secret (password, token, unlock_token, api key, ...) is
  printed as ``[redacted]`` -- no head, no length, no hash. A hash of a weak password is a
  guessing oracle.
* Every other argument is printed as ``head (len N, #hash8)``. The head is at most one third of
  the value and at most 8 characters, so a short value that happens to be a secret is never shown
  whole. Three kinds of value are exempt because they are structure, not content: booleans and
  small integers, and the value of a path-like argument (``path``, ``file``, ``pattern`` ...),
  which is shown up to 48 characters because "which file was read" is the question a trace
  exists to answer.
* The text of a result is shown as a short head only, passed through ``scrub``. When any
  argument of the call mentions ``.env``, or the tool is one whose result is a secret by nature,
  no head is shown at all.
* ``scrub`` removes ``key=value`` / ``key: value`` pairs whose key looks secret, upper-case
  ``NAME=value`` assignments (the shape of a ``.env`` file), bearer tokens, well-known token
  prefixes, and any long mixed letter/digit run that looks like a credential.
"""
from __future__ import annotations

import re

#: Argument names whose value is never printed. Superset of tools.tool_ledger.SECRET_ARGS.
SECRET_ARG_NAMES = {
    "password", "passwd", "pwd", "pass", "secret", "token", "unlock_token", "api_key", "apikey",
    "authorization", "auth", "credential", "credentials", "private_key", "access_key",
    "secret_key", "cookie", "session_token", "bearer",
}
_SECRET_NAME_PARTS = ("password", "passwd", "secret", "token", "apikey", "api_key",
                      "credential", "private_key", "authorization", "cookie")

#: Argument names that hold a place (a path, a glob, a name), shown longer than other values.
PATH_ARG_NAMES = {
    "path", "paths", "file", "files", "filename", "file_path", "src", "dst", "source", "dest",
    "destination", "working_dir", "cwd", "dir", "directory", "folder", "pattern", "glob",
    "output_path", "target", "name", "tool", "category", "claim_id", "job_id", "run_id",
}

#: Tools whose result is a secret (or is sensitive by nature); the result head is withheld.
WITHHELD_RESULT_TOOLS = {
    "unlock", "office_password_recovery", "clipboard_get", "registry_read", "odbc_query",
    "odbc_tables", "odbc_columns", "outlook_inbox", "outlook_calendar", "env_info",
    "secret_get", "list_unlocked",
}

WITHHELD = "[withheld]"
REDACTED = "[redacted]"

_SECRET_KEY = r"[A-Za-z0-9_.\-]*(?:pass(?:word|wd)|pwd|secret|token|api[_-]?key|credential|" \
              r"private[_-]?key|authorization|cookie)[A-Za-z0-9_.\-]*"
_PAIR_RE = re.compile(r"(?i)\b(" + _SECRET_KEY + r")(\s*[:=]\s*|\s+is\s+)(\"[^\"]*\"|'[^']*'|\S+)")
_ENV_LINE_RE = re.compile(r"(?m)\b([A-Z][A-Z0-9_]{2,})=(\"[^\"]*\"|'[^']*'|[^\s\"']+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_PREFIXED_RE = re.compile(r"\b(?:(?:sk|pk|rk)-[-_A-Za-z0-9]{8,}|(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{8,}"
                          r"|xox[abprs]-[-A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|AIza[-_A-Za-z0-9]{20,}"
                          r"|ya29\.[-_A-Za-z0-9.]{8,})")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}(?:\.[A-Za-z0-9_\-]*)?")
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9+_\-]{24,}={0,2}")
_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _looks_like_credential(run: str) -> bool:
    has_digit = any(c.isdigit() for c in run)
    has_alpha = any(c.isalpha() for c in run)
    if not (has_digit and has_alpha):
        return False
    # a hex digest or a long identifier is not a person's secret, but it is also not
    # informative in a trace -- hiding it costs nothing.
    return True


def scrub(text) -> str:
    """Return `text` with anything that looks like a credential replaced."""
    if text is None:
        return ""
    s = str(text)
    s = _PAIR_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, s)
    s = _ENV_LINE_RE.sub(lambda m: "%s=%s" % (m.group(1), REDACTED), s)
    s = _BEARER_RE.sub(lambda m: "%s %s" % (m.group(1), REDACTED), s)
    s = _JWT_RE.sub(REDACTED, s)
    s = _PREFIXED_RE.sub(REDACTED, s)
    s = _LONG_RUN_RE.sub(lambda m: REDACTED if _looks_like_credential(m.group(0)) else m.group(0), s)
    s = _ADDRESS_RE.sub("[address]", s)
    return s


def flat(text, limit: int) -> str:
    """Whitespace flattened to single spaces and cut to `limit` characters."""
    s = " ".join(str(text or "").split())
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def is_secret_name(name) -> bool:
    low = str(name or "").strip().lower()
    if low in SECRET_ARG_NAMES:
        return True
    return any(part in low for part in _SECRET_NAME_PARTS)


_SIMPLE_VALUE_RE = re.compile(r"^(?:true|false|none|null|\d{1,6})$", re.I)


def mentions_env(text) -> bool:
    return ".env" in str(text or "").lower()


def summarize_value(name, spec) -> str:
    """One argument as ``name=head (len N, #hash8)``. `spec` is the ledger's bounded dict."""
    name = scrub(flat(name, 40))
    if is_secret_name(name):
        return "%s=%s" % (name, REDACTED)
    if isinstance(spec, dict) and spec.get("redacted"):
        return "%s=%s" % (name, REDACTED)
    if isinstance(spec, dict) and "text" in spec:
        text = str(spec.get("text") or "")
        length = spec.get("len")
        if not isinstance(length, int):
            length = len(text)
        sha = str(spec.get("sha16") or "")[:8]
    else:
        text = str(spec if spec is not None else "")
        length = len(text)
        sha = ""
    if _SIMPLE_VALUE_RE.match(text.strip()):
        return "%s=%s" % (name, text.strip())
    low = str(name).lower()
    if low in PATH_ARG_NAMES or low.endswith(("_path", "_dir", "_file")):
        keep = min(48, len(text))
    else:
        keep = min(8, length // 3, len(text))
    head = scrub(" ".join(text[:keep].split())) if keep > 0 else ""
    tail = "len %d" % length
    if sha:
        tail += ", #%s" % sha
    return "%s=%s%s (%s)" % (name, head, "…" if (keep < length and head) else "", tail)


def summarize_args(args) -> list:
    """The call's arguments as a list of redacted ``name=...`` strings (nested dicts dotted)."""
    out = []

    def walk(prefix, mapping, depth):
        if not isinstance(mapping, dict):
            out.append(summarize_value(prefix or "_", mapping))
            return
        for key, val in mapping.items():
            full = "%s.%s" % (prefix, key) if prefix else str(key)
            is_spec = isinstance(val, dict) and ("text" in val or "redacted" in val)
            if isinstance(val, dict) and not is_spec and depth < 4:
                if is_secret_name(key):
                    out.append("%s=%s" % (scrub(flat(full, 40)), REDACTED))
                else:
                    walk(full, val, depth + 1)
            else:
                out.append(summarize_value(full, val))

    walk("", args if isinstance(args, dict) else {"_": args}, 0)
    return out[:12]


def args_mention_env(args) -> bool:
    """True when any argument text mentions .env (used to withhold the result head)."""
    found = []

    def walk(v):
        if isinstance(v, dict):
            if "text" in v and isinstance(v.get("text"), str):
                found.append(v["text"])
            for x in v.values():
                if isinstance(x, (dict, list)):
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            found.append(v)

    walk(args)
    return any(mentions_env(t) for t in found)


def result_head(tool, args, text, limit: int = 80) -> str:
    """A short, scrubbed head of a result, or WITHHELD when the call is sensitive."""
    if not text:
        return ""
    if str(tool) in WITHHELD_RESULT_TOOLS or args_mention_env(args):
        return WITHHELD
    return scrub(flat(text, limit))
