"""tools/backfill_conversation_ids.py: dry run writes nothing; --apply sets only empty urls whose
transcript records a guid, leaves worker-name twins alone, and proves its own counts."""
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_conversation_ids as b  # noqa: E402


def _tx(path, guid=None, gz=False):
    lines = [json.dumps({"meta": True, "name": "w0"}), json.dumps({"turn": 1, "role": "user", "text": "x"})]
    if guid:
        lines.append(json.dumps({"guid": guid}))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    (gzip.open if gz else open)(str(path), "wb").write(data)
    return str(path)


def _setup(tmp_path):
    t = tmp_path / "transcripts"
    t.mkdir()
    rows = [
        {"url": "sess:aaaa", "name": "w0", "source": "fleet", "transcript": _tx(t / "r1_a0_w0.jsonl", "aaaa")},
        {"url": "", "name": "w0", "source": "fleet", "transcript": _tx(t / "r2_a0_w0.jsonl", "bbbb")},
        {"url": "", "name": "w0", "source": "fleet", "transcript": _tx(t / "r3_a0_w0.jsonl.gz", "cccc", gz=True)},
        {"url": "", "name": "w0", "source": "fleet", "transcript": _tx(t / "r4_a0_w0.jsonl")},        # no guid
        {"url": "", "name": "w0", "source": "fleet", "transcript": _tx(t / "r5_a0_w0.jsonl", "aaaa")},  # taken
        {"url": "https://x/c/1", "name": "w0", "source": "fleet", "transcript": ""},
    ]
    (tmp_path / "conversations.json").write_text(json.dumps(rows), encoding="utf-8")
    return rows


def test_dry_run_changes_nothing(tmp_path):
    _setup(tmp_path)
    before = (tmp_path / "conversations.json").read_bytes()
    assert b.run(str(tmp_path), apply=False, out=lambda m: None) == 0
    assert (tmp_path / "conversations.json").read_bytes() == before
    assert not [p for p in os.listdir(tmp_path) if ".bak" in p]


def test_apply_sets_only_what_the_transcript_records(tmp_path):
    rows = _setup(tmp_path)
    log = []
    assert b.run(str(tmp_path), apply=True, out=log.append) == 0
    got = json.loads((tmp_path / "conversations.json").read_text(encoding="utf-8"))
    assert [r["url"] for r in got] == ["sess:aaaa", "sess:bbbb", "sess:cccc", "", "", "https://x/c/1"]
    for old, new in zip(rows, got):                       # nothing but url moved
        assert {k: v for k, v in old.items() if k != "url"} == {k: v for k, v in new.items() if k != "url"}
    assert len([p for p in os.listdir(tmp_path) if ".bak-" in p]) == 1
    assert any("after:" in m and "'empty_url': 2" in m for m in log), log
    assert not (tmp_path / "conversations.json").read_bytes().startswith(b"\xef\xbb\xbf")


def test_a_second_apply_is_a_no_op(tmp_path):
    _setup(tmp_path)
    b.run(str(tmp_path), apply=True, out=lambda m: None)
    first = (tmp_path / "conversations.json").read_bytes()
    assert b.run(str(tmp_path), apply=True, out=lambda m: None) == 0
    assert (tmp_path / "conversations.json").read_bytes() == first
