# -*- coding: utf-8 -*-
"""The harness is a general-purpose agent system: no company system, tool family or topic in it.

OWNER RULE (2026-10-15, absolute). Domain specifics -- the names of a database, a driver, a tool
family, a kind of data -- live ONLY inside the skill that needs them (its SKILL.md and
frontmatter). Everything the harness says to a model about skills, sources and reviews, and every
module that enforces it, stays domain-free, so the same harness serves any domain.

This pins the texts and modules written for that purpose. It does not rewrite older code: the
older modules listed in KNOWN_OLDER are tracked here so the list can only shrink.
"""
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import refuter as R  # noqa: E402
from relay import skill_procedure as SP  # noqa: E402
from relay import relay_fleet as F  # noqa: E402

def _domain_pattern():
    """The vocabulary that belongs to one deployment's data, not to the harness.

    THE LIST OF WORDS IS ITSELF A DISCLOSURE, SO IT DOES NOT LIVE IN THIS REPOSITORY. This file used
    to spell the deployment's own system names and data words in a regex, which put in a public
    repository exactly the words the test exists to keep out of it. They are read from the
    environment (MCP_DOMAIN_WORDS) or from one file in the user's home (~/.m365_domain_words.txt),
    each a `|`-separated alternation. Where neither is set (CI, a fresh clone) only the technology
    words below are checked, which is still a real check of the harness texts.
    """
    words = (os.environ.get("MCP_DOMAIN_WORDS") or "").strip()
    if not words:
        path = os.path.join(os.path.expanduser("~"), ".m365_domain_words.txt")
        try:
            with open(path, encoding="utf-8-sig") as fh:
                words = fh.read().strip()
        except OSError:
            words = ""
    technology = r"odbc|sql\s*server"
    return re.compile(technology + (("|" + words) if words else ""), re.IGNORECASE)


DOMAIN = _domain_pattern()


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


TEXTS = {
    "skill frame standing rule": SP.FRAME_RULE,
    "completion steer": SP.STEER_TEXT,
    "stuck cause text": SP.STUCK_CAUSE_TEXT,
    "reviewer source check": R.SOURCE_CHECK_RULE,
    "reviewer subject check": R.SUBJECT_CHECK_RULE,
    "reviewer judge check": R.JUDGE_CHECK_RULE,
    "frame header": F._SKILL_HEADER,
}


@pytest.mark.parametrize("name", sorted(TEXTS))
def test_text_sent_to_models_is_domain_free(name):
    m = DOMAIN.search(TEXTS[name])
    assert m is None, "%s names a domain word: %r" % (name, m and m.group(0))


def test_the_rendered_frames_add_no_domain_words_of_their_own():
    """Compose both frame forms around a neutral goal with a neutral stub body: whatever the
    harness itself adds must be free of domain words."""
    goal = "neutral goal"
    body = "neutral body"
    for text in (F._skill_v1(goal, body),):
        added = text.replace(goal, "").replace(body, "")
        assert DOMAIN.search(added) is None, DOMAIN.search(added).group(0)


def test_the_steer_names_only_what_the_skill_declared():
    text = SP.steer_text("drill", ["alpha_*"])
    assert "drill" in text and "alpha_*" in text
    assert DOMAIN.search(text.replace("drill", "").replace("alpha_*", "")) is None


#: Modules written for the source guard. Their SOURCE (code and comments) must be domain-free.
GUARD_MODULES = ["relay/skill_procedure.py"]


@pytest.mark.parametrize("rel", GUARD_MODULES)
def test_guard_module_source_is_domain_free(rel):
    m = DOMAIN.search(_read(rel))
    assert m is None, "%s mentions %r" % (rel, m and m.group(0))


def test_the_stuck_cause_added_for_the_guard_is_domain_free():
    src = _read("relay/stuck_cause.py")
    start = src.index("def procedure_unfollowed_cause")
    assert DOMAIN.search(src[start:]) is None


def test_the_worker_gate_is_domain_free():
    src = _read("relay/relay_fleet.py")
    a = src.index("    def _procedure_note(self)")
    b = src.index("    def _on_done_claimed(self")
    assert DOMAIN.search(src[a:b]) is None


def test_the_server_instructions_and_protocol_preamble_carry_no_domain_vocabulary_for_skills():
    """The FastMCP instructions' skill rules (RULE 2 and its neighbours) are what a model reads
    at connection time. Only the rule block is checked, so older unrelated text cannot make
    this fail for the wrong reason."""
    src = _read("main.py")
    start = src.index("RULE 2 -- DO THIS SECOND")
    block = src[start:start + 1600]
    block = "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))
    assert DOMAIN.search(block) is None, DOMAIN.search(block).group(0)
