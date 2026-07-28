"""A malformed SKILL.md must be reported, not silently skipped.

Discovery used to swallow SkillError and `continue`, so a Skill whose frontmatter
did not parse simply never appeared: not in any listing, with no error raised
anywhere. The commonest cause is an unquoted ':' inside the description, which is
easy to write and invisible to the author -- the Skill just does not exist.

Run: pytest -q relay/test_skill_invalid_reporting.py
"""
from __future__ import annotations

import pytest

from relay.skills import SkillError, SkillStore


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


GOOD = """---
name: good-skill
description: "A working Skill: the colon here is quoted."
---

Body.
"""

# Unquoted ':' in the description -> invalid YAML mapping. This is the real-world case.
BROKEN = """---
name: broken-skill
description: 設備点検記録のExcel（列: 時刻 / 判定）から月次サマリを作る
---

Body.
"""


@pytest.fixture()
def store(tmp_path):
    _write(tmp_path / "skills" / "good-skill" / "SKILL.md", GOOD)
    _write(tmp_path / "skills" / "broken-folder" / "SKILL.md", BROKEN)
    return SkillStore(tmp_path, db_path=tmp_path / "skills.sqlite3",
                      gate_dir=tmp_path / "gates")


def test_valid_skill_is_discovered_and_broken_one_is_not(store):
    names = [s.name for s in store.discover()]
    assert "good-skill" in names
    assert "broken-skill" not in names          # never partially exposed
    assert "broken-folder" not in names


def test_broken_bundle_is_reported_with_its_reason(store):
    invalid = store.invalid_bundles()
    assert "broken-folder" in invalid           # keyed by folder
    assert "broken-skill" in invalid            # and by the name the file declares
    assert "yaml" in invalid["broken-folder"].lower()


def test_get_explains_that_the_skill_exists_but_failed_to_load(store):
    with pytest.raises(SkillError) as excinfo:
        store.get("broken-skill")
    message = str(excinfo.value)
    assert "exists on disk" in message
    assert "YAML" in message or "yaml" in message


def test_get_still_reports_a_genuinely_unknown_skill_plainly(store):
    with pytest.raises(SkillError) as excinfo:
        store.get("never-created")
    assert "unknown Skill" in str(excinfo.value)


def test_invalid_bundles_triggers_discovery_when_called_first(tmp_path):
    """invalid_bundles() must work without an explicit discover() beforehand."""
    _write(tmp_path / "skills" / "broken-folder" / "SKILL.md", BROKEN)
    fresh = SkillStore(tmp_path, db_path=tmp_path / "skills.sqlite3",
                       gate_dir=tmp_path / "gates")
    assert "broken-folder" in fresh.invalid_bundles()
