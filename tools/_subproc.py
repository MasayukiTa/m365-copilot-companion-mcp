"""Shared subprocess hardening helpers (MCP spec §21.6.4 — minimal-permission env).

Used by tools/code_exec.py (run_python, shell_exec) and tools/jobs.py
(run_in_background, run_python_in_background) so all four subprocess spawn
sites build the child environment the same way instead of four copy-pasted
implementations.

Approach: DENYLIST, not allowlist. An allowlist risks silently breaking the
user's own scripts (arbitrary env vars they rely on). We start from a full
copy of os.environ (so PATH / SystemRoot / TEMP / PYTHONPATH / etc. keep
working — including sys.executable resolution on Windows) and strip out
anything that looks like a secret:

  * keys matching ^MCP_          (MCP_API_KEY, MCP_UNLOCK_PASSWORD, MCP_DB_*,
                                   MCP_TUNNEL_*, MCP_TOOL_MAP*, ...)
  * HF_TOKEN
  * keys ending in _AGENT_URL, _TOKEN
  * keys containing PASSWORD, SECRET, API_KEY
  * every key literally present in the repo's .env file (project-specific
    catch-all; read is best-effort and guarded — a missing/unreadable .env
    just means this extra step is skipped, the pattern denylist above still
    applies)
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"

_DENY_PATTERNS = [
    re.compile(r"^MCP_", re.IGNORECASE),
    re.compile(r"^HF_TOKEN$", re.IGNORECASE),
    re.compile(r"_AGENT_URL$", re.IGNORECASE),
    re.compile(r"_TOKEN$", re.IGNORECASE),
    re.compile(r"PASSWORD", re.IGNORECASE),
    re.compile(r"SECRET", re.IGNORECASE),
    re.compile(r"API_KEY", re.IGNORECASE),
    # NAMES THAT CARRY CREDENTIALS WITHOUT SAYING SO. A review named several the list above
    # misses entirely: PIP_INDEX_URL routinely holds https://user:credential@host/simple, and
    # the same shape appears in package-index, database, cloud and container config. None of
    # them contains "password", "secret" or "api_key", so all of them reached the child while
    # the module's docstring said secrets were stripped.
    re.compile(r"(?:^|_)(?:PAT|JWT|CREDENTIALS?|AUTH|BEARER|SESSION_KEY)(?:$|_)",
               re.IGNORECASE),
    re.compile(r"^(?:PIP|UV|POETRY|NPM|YARN)_.*(?:URL|AUTH|TOKEN|PASSWORD|KEYRING)",
               re.IGNORECASE),
    re.compile(r"^(?:DATABASE|DB|POSTGRES|MYSQL|MONGO|REDIS|AMQP)_?URL$", re.IGNORECASE),
    re.compile(r"CONNECTION_STRING", re.IGNORECASE),
    re.compile(r"^(?:AWS|AZURE|GCP|GOOGLE|GH|GITHUB|GITLAB|OPENAI|ANTHROPIC)_",
               re.IGNORECASE),
    re.compile(r"^(?:KUBECONFIG|DOCKER_AUTH_CONFIG|SSH_AUTH_SOCK|NETRC)$", re.IGNORECASE),
    re.compile(r"^(?:HTTP|HTTPS|ALL)_PROXY$", re.IGNORECASE),   # may embed userinfo
]

#: A VALUE THAT LOOKS LIKE A CREDENTIAL, whatever it is called. The list above is still a
#: denylist, and a denylist cannot be finished -- the next deployment invents a variable name
#: nobody here thought of. This catches the shape instead: URI userinfo (`scheme://user:pw@`)
#: is how most of the misses above actually carry their secret, so a variable holding one is
#: withheld regardless of its name.
_USERINFO_IN_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")


def _env_file_keys(env_path: Optional[Path] = None) -> set[str]:
    """Best-effort parse of KEY=... lines from the repo .env file.

    Guarded: a missing file, a permissions error, or malformed lines just
    result in an empty (or partial) set — never raises.
    """
    path = env_path or _ENV_FILE
    keys: set[str] = set()
    try:
        if not path.is_file():
            return keys
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return keys
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m:
            keys.add(m.group(1))
    return keys


def sanitized_child_env() -> dict[str, str]:
    """Return a copy of the current process environment with secrets stripped.

    Keeps PATH, SystemRoot/SYSTEMROOT, TEMP/TMP, USERPROFILE, PYTHONPATH, and
    anything else not matched by the denylist so legitimate child scripts
    (including `sys.executable` resolution on Windows) keep working.
    """
    env = dict(os.environ)
    deny_literal = _env_file_keys()

    for key in list(env.keys()):
        if key in deny_literal:
            del env[key]
            continue
        if any(pat.search(key) for pat in _DENY_PATTERNS):
            del env[key]
            continue
        # BY VALUE AS WELL AS BY NAME. A denylist of names is only ever as complete as the
        # last person to think about it; a URL with userinfo in it is a credential no matter
        # what the variable is called.
        if _USERINFO_IN_URL.search(env.get(key) or ""):
            del env[key]
    return env
