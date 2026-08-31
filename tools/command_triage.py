"""Which commands are exempt from judgement, and which mode the layer is running in.

TWO THINGS DECIDE WHETHER A JUDGE IS CALLED AT ALL.

First, an exemption for commands whose effect is provably read-only. This is the whole answer
to latency: a judging call in front of `git status` costs a round trip and buys nothing, and a
layer that makes every trivial command slow is a layer people switch off. The exemption list is
therefore short, exact, and argued -- not "things that look safe".

Second, the mode. Enforcement starts in shadow: the verdict is recorded and nothing is blocked.
Switching a gate from permissive to closed without measuring first is a mistake this repository
has already been corrected for once, and the research this design follows recommends the same
order -- shadow, review a deliberately adversarial set, then enforce.

    MCP_JUDGE_MODE=off       no judgement at all
    MCP_JUDGE_MODE=shadow    judge, record, never block          (default)
    MCP_JUDGE_MODE=enforce   judge, record, and act on it
"""
from __future__ import annotations

import os
import re
import shlex
from typing import List, Tuple

MODE_ENV = "MCP_JUDGE_MODE"


def mode() -> str:
    m = (os.environ.get(MODE_ENV) or "shadow").strip().lower()
    return m if m in ("off", "shadow", "enforce") else "shadow"


#: Commands whose effect is reading. Matched on the FIRST WORD ONLY, after the shell operators
#: below have been ruled out -- `git status` is read-only, `git status && rm -rf x` is not, and
#: a list matched against the whole string would exempt the second because it starts with the
#: first.
_READ_ONLY_HEADS = frozenset("""
    ls dir pwd cd echo cat type head tail wc find findstr grep rg sort uniq
    git-status git-diff git-log git-show git-branch
    python-V node-v npm-v pip-V where which whoami hostname date
""".split())

#: `git <sub>` and `npm <sub>` are only read-only for particular subcommands, so they are keyed
#: on the pair. Anything not named here goes to the judge, including plain `git`.
_READ_ONLY_PAIRS = frozenset([
    ("git", "status"), ("git", "diff"), ("git", "log"), ("git", "show"),
    ("git", "branch"), ("git", "remote"), ("git", "rev-parse"), ("git", "ls-files"),
    ("npm", "ls"), ("npm", "view"), ("npm", "outdated"),
    ("pip", "list"), ("pip", "show"), ("pip", "freeze"),
    ("docker", "ps"), ("docker", "images"),
])

#: Anything that chains, redirects, substitutes or expands can change what the first word does.
#: A command containing one of these is never exempt, whatever it starts with.
_COMPOSITION = re.compile(r"[|;&><`$]|\|\||&&|\$\(|\bstart\b", re.IGNORECASE)

#: READING IS NOT AUTOMATICALLY HARMLESS, and this list is the correction of a defect measured
#: in this file's own first version. An adversarial pass on 2026-08-31 put fifty commands
#: through the layer; exactly three reached execution having never been judged at all, and all
#: three were the same shape:
#:
#:     cat ~/.ssh/id_rsa      -> exempt ("cat reads a file")
#:     type .env              -> exempt ("type reads a file")
#:     cat .git-credentials   -> exempt ("cat reads a file")
#:
#: Every other miss in that run was a miss by the DETERMINISTIC net, which is the expected and
#: intended division of labour -- the judge exists for those. These three were different in
#: kind: the exemption above removed them from judgement entirely, so no layer of the design
#: could see them. A silent bypass is the only failure this file can produce, and it produced
#: it on the class that matters most.
#:
#: The reasoning error was treating "does not write" as "has no effect". Reading a private key
#: copies it into the transcript, and from there it travels wherever the transcript travels.
#: tools/file_ops.py already refuses `.env` and `.companion_gates` outright; a shell that will
#: cat them is that refusal with a way around it.
#:
#: Deliberately tight. A path that merely LOOKS security-adjacent (`relay/profile_token.py`)
#: costs one judging call if it lands here, which is the cheap direction -- but a list wide
#: enough to catch every such name would make ordinary source reading slow, and slow is how a
#: layer gets switched off.
_SENSITIVE_TARGET = re.compile(
    r"(?:^|[\s\"'=])~?[^\s\"']*(?:"
    r"\.ssh[/\\]"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|\.git-credentials"
    r"|\.companion_gates"
    r"|\.npmrc|\.pypirc|\.netrc"
    r"|\.aws[/\\]|\.kube[/\\]|\.docker[/\\]config"
    r"|\.(?:pem|pfx|p12|jks|keystore)\b"
    r"|(?:^|[/\\.])(?:credentials?|secrets?)\b"
    # `.env` and its variants, ending at a word boundary rather than at end-of-string: the
    # first version anchored with `$` and therefore did not match `type .env`, which is the
    # exact command the guard was written for. Measured -- it was still exempt after the fix
    # that was supposed to cover it.
    r"|\.env(?:\.[A-Za-z0-9_-]+)?(?=$|[\s\"'])"
    r")", re.IGNORECASE)


def reads_something_sensitive(command: str) -> bool:
    """True when the command names a credential-shaped path. Never exempt those from judgement."""
    return bool(_SENSITIVE_TARGET.search(command or ""))


def is_read_only(command: str) -> Tuple[bool, str]:
    """(exempt, why). Conservative: anything not understood is NOT exempt.

    The failure direction matters. A wrong "exempt" skips the judge silently; a wrong "not
    exempt" costs one judging call. Only the first is dangerous, so ambiguity resolves against
    exemption.
    """
    if not command or not command.strip():
        return False, "empty"
    if _COMPOSITION.search(command):
        return False, "composed: a pipe, chain, redirect or substitution can change the effect"
    if reads_something_sensitive(command):
        # Checked BEFORE the head lookup, so it applies to every exempt reader at once rather
        # than to whichever ones someone remembered. See _SENSITIVE_TARGET.
        return False, "names a credential-shaped path: reading it is an effect"
    try:
        parts: List[str] = shlex.split(command, posix=False)
    except ValueError:
        return False, "unparseable quoting"
    if not parts:
        return False, "empty"

    head = parts[0].strip('"').lower()
    head = head.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if head.endswith(".exe"):
        head = head[:-4]

    if len(parts) >= 2:
        sub = parts[1].strip('"').lower()
        if (head, sub) in _READ_ONLY_PAIRS:
            return True, "%s %s reads" % (head, sub)
    if head in _READ_ONLY_HEADS and len(parts) == 1:
        return True, "%s alone reads" % head
    if head in ("ls", "dir", "pwd", "whoami", "hostname", "date", "where", "which"):
        return True, "%s reads" % head
    if head in ("cat", "type", "head", "tail", "wc", "findstr", "grep", "rg") and len(parts) >= 2:
        return True, "%s reads a file" % head
    if head in ("pytest", "python", "node") and len(parts) >= 1:
        # NOT exempt. `pytest` is the most common command here and exempting it is tempting,
        # but a test suite runs arbitrary project code and `python x.py` runs anything at all.
        # The point of the layer is the effect, not the name at the front.
        return False, "%s runs arbitrary code" % head
    return False, "not on the read-only list"
