# -*- coding: utf-8 -*-
"""The template must state what a STANDARD (default) setup gets for the Dev Tunnel.

WHY THIS EXISTS. The owner asked, of the env/tunnel work, "standard no kankyou settei wo shita
hito wa dou naru?" -- what happens to someone who left this template unchanged. The answer was
implemented (PR #295: host_tunnel_for_external_clients, ON by default, hosts a tunnel for an
external client even on the connector-less default route) and declared in tools/settings_keys.py,
but a person reading .env.example to decide what their machine does could not find it: the Dev
Tunnel section spoke only of anonymous access. The conclusion now lives in the template, and this
test keeps it true to the implementation so the two cannot drift.

The assertions below bind the TEMPLATE PROSE to the CODE DEFAULTS, not to any particular wording,
so an edit that changes the default (say, host_tunnel_for_external_clients off by default) fails
here until the template is corrected too. It reads files only; it is portable across OSes.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _env_example_text():
    return (ROOT / ".env.example").read_text(encoding="utf-8")


def _standard_setup_section(text):
    """The paragraph that answers 'what does a standard (default) setup get'.

    Anchored on an explicit heading so the test is about THAT paragraph, not an incidental
    mention of the words elsewhere in the file.
    """
    marker = "WHAT A STANDARD (DEFAULT) SETUP GETS."
    assert marker in text, (
        ".env.example must have a paragraph that states what a standard/default setup gets "
        "for the Dev Tunnel (the owner's env/tunnel question in row #32)."
    )
    start = text.index(marker)
    # the section is the comment block from the marker up to the next blank (non-comment) line
    lines = text[start:].splitlines()
    out = []
    for ln in lines:
        if out and not ln.startswith("#"):
            break
        out.append(ln)
    return "\n".join(out)


def test_the_section_names_the_connector_less_default_route():
    section = _standard_setup_section(_env_example_text())
    # a standard setup runs the connector-less route; the section must say so, and point at the doc
    assert "CONNECTOR-LESS" in section.upper()
    assert "connector_less_route.md" in section
    # and it must say the agent itself needs no tunnel / no public URL on that route
    low = section.lower()
    assert "no tunnel" in low
    assert "public url" in low


def test_the_section_agrees_with_the_host_tunnel_default_being_on():
    from relay import code_staleness

    # Guard the facts the prose leans on. If these change, the prose below is no longer true.
    assert code_staleness.HOST_TUNNEL_DEFAULT == "on"
    assert code_staleness.HOST_TUNNEL_MODES == ("off", "on")
    assert code_staleness.HOST_TUNNEL_KEY == "host_tunnel_for_external_clients"

    section = _standard_setup_section(_env_example_text())
    # the prose must name the setting and state it is ON by default (that is what gives a
    # standard, connector-less setup a hosted tunnel for an external client)
    assert "host_tunnel_for_external_clients" in section
    low = section.lower()
    assert "on by default" in low
    assert "external client" in low
    # and the off case (a connector-less PC then hosts no tunnel) must be stated, matching the
    # declared modes
    assert "off" in low


def test_the_section_agrees_with_the_settings_declaration():
    from tools import settings_keys

    key = settings_keys.KEYS["host_tunnel_for_external_clients"]
    assert key.default == "on"
    section = _standard_setup_section(_env_example_text())
    # the prose points the reader at where the toggle lives, matching the declaration's home
    assert "settings_keys.py" in section


def test_the_section_says_a_changing_public_url_is_not_a_fault_for_a_standard_setup():
    # Row #33 follow-up: for the standard (initial) state the public URL is allowed to change
    # across restarts and that is not a problem, because nothing a standard setup runs depends
    # on the URL being stable. The template must say this so the reader is not alarmed by it.
    section = _standard_setup_section(_env_example_text())
    low = section.lower()
    assert "public url" in low
    assert "change" in low
    assert "not a fault" in low or "not a problem" in low


def test_a_connector_route_machine_is_called_out_as_unaffected():
    # A PC already on the connector route (it has MCP_TUNNEL_URL / MCP_TUNNEL_NAME) must be
    # named as unaffected, so a migrated user does not read the standard-setup answer as theirs.
    section = _standard_setup_section(_env_example_text())
    assert "MCP_TUNNEL_URL" in section or "MCP_TUNNEL_NAME" in section
    assert "connector route" in section.lower()
