"""Nothing private may enter the archive that gets pushed.

Archive() with no argument writes to relay/selfimprove/archive/entries.jsonl, which is
tracked in a public repository, and scheduler.nightly() -- reached from its own CLI with no
archive_path -- takes that default. So the loop's ordinary operation appends live genomes to
a file that is published. The file holds two benchmark rows today and nothing has escaped;
the route was simply open.

A test already audited the published file. Auditing is the wrong moment: it runs after the
commit exists, so the first person to learn would be whoever pulled it. These cover the
refusal at the write, which is the only point where writing somewhere else is still possible.

Run: pytest -q relay/selfimprove/test_archive_publish_guard.py
"""
import pytest

from relay.selfimprove.archive import Archive, NotPublishable, _DEFAULT_ARCHIVE

BENCH = "instance_NodeBB__NodeBB-70b4a0e2aebe-vnan"
VERIFIED = "astropy__astropy-13453"
GENOME = {"knobs": {"batch": 8}, "cards": {"interface_first": True}, "parent_id": None}


@pytest.fixture
def published(tmp_path, monkeypatch):
    """The published path, redirected at the file but not at the rule, so a refusal is proved
    without writing to the real archive."""
    fake = tmp_path / "entries.jsonl"
    import relay.selfimprove.archive as A
    monkeypatch.setattr(A, "_DEFAULT_ARCHIVE", str(fake))
    return Archive(str(fake))


def test_a_business_episode_id_is_refused(published):
    with pytest.raises(NotPublishable):
        published.add(GENOME, slice_ids=["episode_2026_08_customer_call"], pass_at_1=0.5)


def test_the_refusal_happens_before_anything_is_written(published, tmp_path):
    with pytest.raises(NotPublishable):
        published.add(GENOME, slice_ids=["episode_2026_08"], pass_at_1=0.5)
    assert published.all() == []


def test_the_refusal_says_where_to_write_instead(published):
    """A guard that only says no leaves the caller with nothing to do."""
    with pytest.raises(NotPublishable) as e:
        published.add(GENOME, slice_ids=["episode_2026_08"], pass_at_1=0.5)
    assert "archive_path" in str(e.value)


def test_card_text_is_refused_even_with_public_slices(published):
    """.gitignore predicts genome.cards will hold learned prompt text. Prompt text learned
    from real work quotes real work, so it is the leak that arrives without anyone noticing
    the slice ids were fine."""
    with pytest.raises(NotPublishable):
        published.add({"knobs": {}, "cards": {"learned_opening": "some prose"}, "parent_id": None},
                      slice_ids=[BENCH], pass_at_1=0.5)


def test_card_flags_are_still_publishable(published):
    published.add(GENOME, slice_ids=[BENCH, VERIFIED], pass_at_1=0.5)
    assert len(published.all()) == 1


def test_both_public_benchmark_shapes_pass(published):
    published.add(GENOME, slice_ids=[VERIFIED], pass_at_1=0.5)
    published.add(GENOME, slice_ids=[BENCH], pass_at_1=0.5)
    assert len(published.all()) == 2


def test_a_runtime_archive_is_not_restricted(tmp_path):
    """The restriction is a property of the destination, not of the data: the loop has to be
    able to record what it actually did somewhere."""
    arc = Archive(str(tmp_path / "runtime.jsonl"))
    arc.add({"knobs": {}, "cards": {"learned_opening": "prose"}, "parent_id": None},
            slice_ids=["episode_2026_08_customer_call"], pass_at_1=0.5)
    assert len(arc.all()) == 1


def test_the_live_archive_still_satisfies_its_own_rule():
    """The two rows already published are benchmark records, so the guard does not retroactively
    condemn the file it protects."""
    import json
    import os
    if not os.path.exists(_DEFAULT_ARCHIVE):
        pytest.skip("no published archive in this checkout")
    with open(_DEFAULT_ARCHIVE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            cards = (e.get("genome") or {}).get("cards") or {}
            assert not [k for k, v in cards.items() if isinstance(v, str)]


def test_a_differently_cased_path_is_the_same_file(tmp_path, monkeypatch):
    r"""abspath alone calls RELAY\... and relay\... different files while the filesystem
    calls them the same one, so a caller differing only in case wrote straight past the
    refusal. A permission check that fails open on spelling."""
    import os

    import relay.selfimprove.archive as A
    fake = tmp_path / "entries.jsonl"
    monkeypatch.setattr(A, "_DEFAULT_ARCHIVE", str(fake))

    # THE GUARD IS ABOUT CASE-INSENSITIVE FILESYSTEMS, so on a case-SENSITIVE one there is
    # nothing for it to catch: the uppercased path really is a different file, and refusing it
    # would be the bug. Asserting the refusal anyway made this fail on Linux with a
    # PermissionError from writing /HOME/RUNNER/... -- invisible for as long as the manifest
    # audit stopped the job before pytest ran.
    probe = tmp_path / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("case-sensitive filesystem; this guard has nothing to catch here")

    shouty = str(fake).replace(str(tmp_path.name), str(tmp_path.name).upper()) \
        if os.name == "nt" else str(fake).upper()
    with pytest.raises(NotPublishable):
        Archive(shouty).add(GENOME, slice_ids=["episode_business"], pass_at_1=0.5)


def test_a_genuinely_different_path_is_still_unrestricted(tmp_path, monkeypatch):
    import relay.selfimprove.archive as A
    monkeypatch.setattr(A, "_DEFAULT_ARCHIVE", str(tmp_path / "entries.jsonl"))
    other = Archive(str(tmp_path / "runtime.jsonl"))
    other.add(GENOME, slice_ids=["episode_business"], pass_at_1=0.5)
    assert len(other.all()) == 1
