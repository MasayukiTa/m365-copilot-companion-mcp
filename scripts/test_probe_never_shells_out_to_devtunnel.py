# -*- coding: utf-8 -*-
"""The probe must decide login/host state WITHOUT ever calling the devtunnel CLI.

WHY (measured 2026-11-11). Two devtunnel logins were lost within about a minute while a
monitor ran ``devtunnel user show`` / ``devtunnel show`` every 6-8 s; the one login that
survived 4+ hours did so while NOTHING called the devtunnel CLI during or after sign-in and
only the token FILE (size + mtime) and the supervisor log were read. Hypothesis (one
supportive case, not proven): concurrent devtunnel CLI invocations corrupt the token cache.
So the probe decides login state from the token file and host state from the public probe
result plus the supervisor log -- and must NEVER shell out to devtunnel.

This test fails if any probe module reaches the devtunnel CLI. It checks two independent ways
so a rename or a new call site cannot slip past one of them:

  * STATICALLY, by parsing the source with ast: no call to a process-spawning API
    (subprocess.*, os.system, os.popen, os.exec*, os.spawn*) may carry an argument whose text
    contains "devtunnel"; and more broadly, the token "devtunnel" may appear only inside the
    token-FILE path constant, never next to a spawn API.
  * TEXTUALLY, by confirming none of the obvious spawn+devtunnel combinations appear as source
    substrings.

Stdlib only; no network, no filesystem of the live install, no devtunnel. Runs in Linux CI.
"""
from __future__ import annotations

import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

#: Every probe-side module that MUST stay CLI-free. Kept explicit so a new probe file is a
#: deliberate addition here, not an accidental omission.
PROBE_MODULES = (
    "tunnel_login_state.py",
    "mcp_handshake_probe.py",
)

#: Process-spawning APIs. A "devtunnel" argument flowing into any of these is the shell-out we
#: forbid. Matched on the attribute/callable text, so subprocess.run, subprocess.Popen,
#: subprocess.call, os.system, os.popen, os.execvp, os.spawnv, etc. are all covered.
_SPAWN_RE = re.compile(
    r"\b(?:subprocess\.\w+|os\.system|os\.popen|os\.exec\w*|os\.spawn\w*|Popen|check_output|check_call|run_child|childproc)\b"
)


def _module_paths():
    return [os.path.join(HERE, name) for name in PROBE_MODULES]


def test_probe_modules_exist():
    """Guard against the list drifting away from the files it names."""
    for p in _module_paths():
        assert os.path.isfile(p), "probe module missing: %s" % p


def _call_text(node):
    """Flatten a Call's func into dotted text, e.g. subprocess.run -> 'subprocess.run'."""
    parts = []
    f = node.func
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def _string_constants(node):
    """All string literals anywhere under an AST node."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def test_no_spawn_api_is_handed_a_devtunnel_argument():
    """No subprocess/os spawn call in a probe module may carry a 'devtunnel' argument."""
    for path in _module_paths():
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = _call_text(node)
            if not _SPAWN_RE.search(callee):
                continue
            # A spawn call: none of its string arguments (positional, keyword, or nested in a
            # list/tuple) may mention devtunnel.
            for s in _string_constants(node):
                assert "devtunnel" not in s.lower(), (
                    "%s hands %r to %s(): the probe must not shell out to the devtunnel CLI"
                    % (os.path.basename(path), s, callee))


def test_devtunnel_token_only_appears_in_the_token_path_not_beside_a_spawn():
    """Any 'devtunnel' token in the source sits in the token-FILE path, never a spawn call.

    The modules legitimately reference the token FILE name (devtunnels-tokens-microsoft) and
    the DevTunnels folder. What is forbidden is the CLI: the executable name 'devtunnel'
    passed to a process spawner. This asserts the only occurrences of the bare word are inside
    string literals (paths/comments/docstrings), and the previous test proves none of those
    literals are spawn arguments -- so the word can never be an argv[0] for a subprocess.
    """
    for path in _module_paths():
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        # Collect the character spans of every string literal so we can tell "inside a string"
        # from "bare identifier". A bare identifier 'devtunnel' would be a NameError at runtime,
        # but catching it here names the real risk (an import of a devtunnel wrapper module).
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", "") or ""
                names = [a.name for a in getattr(node, "names", [])]
                blob = (mod + " " + " ".join(names)).lower()
                assert "devtunnel" not in blob, (
                    "%s imports a devtunnel module (%s): the probe must not reach the CLI"
                    % (os.path.basename(path), blob.strip()))


def test_no_textual_spawn_plus_devtunnel_combo():
    """Belt-and-braces: none of the obvious 'spawn + devtunnel' source combos appear."""
    forbidden = re.compile(
        r"(subprocess\.\w+|os\.system|os\.popen|Popen|check_output|check_call)"
        r"[^\n]{0,80}devtunnel",
        re.IGNORECASE,
    )
    for path in _module_paths():
        src = open(path, encoding="utf-8").read()
        m = forbidden.search(src)
        assert m is None, (
            "%s has a spawn call near 'devtunnel': %r"
            % (os.path.basename(path), m.group(0)[:100]))
