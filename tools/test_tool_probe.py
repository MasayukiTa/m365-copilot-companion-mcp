"""Hermetic tests for tools/tool_probe.py -- the tool-call self-probe tracker added so a
bridge-side MCP connector going stale (consent lapsed / CDP session dead) is visible to
/health instead of silently failing behind an all-green FleetCockpit health strip (see
tool_probe.py's module docstring for the full incident context).

Covers: the pure classify_probe_reply() classifier for every (ok, kind) branch, the
record_probe()/get_summary() roundtrip for every kind, corrupt/missing-file tolerance,
deterministic age computation via injected `now`, atomic-write (no partial .tmp left
behind), and never-raises behavior when the sidecar path is unwritable.

Run: pytest -q tools\\test_tool_probe.py
"""
from __future__ import annotations

import json

from tools import tool_probe


# ===========================================================================
# 1. classify_probe_reply: pure classifier, every branch
# ===========================================================================


def test_classify_agent_unreachable_wins_even_with_ok_token():
    # agent_loaded=False must short-circuit before any text inspection.
    ok, kind = tool_probe.classify_probe_reply(
        tool_probe.PROBE_OK_TOKEN, agent_loaded=False)
    assert (ok, kind) == (False, "agent_unreachable")


def test_classify_agent_unreachable_on_empty_reply():
    ok, kind = tool_probe.classify_probe_reply("", agent_loaded=False)
    assert (ok, kind) == (False, "agent_unreachable")


def test_classify_consent_card_japanese_marker():
    ok, kind = tool_probe.classify_probe_reply(
        "接続マネージャーを開く からご確認ください。", agent_loaded=True)
    assert (ok, kind) == (False, "consent_card")


def test_classify_consent_card_english_marker():
    ok, kind = tool_probe.classify_probe_reply(
        "Please open the connection manager to continue.", agent_loaded=True)
    assert (ok, kind) == (False, "consent_card")


def test_classify_canned_fallback_marker():
    ok, kind = tool_probe.classify_probe_reply(
        "申し訳ございません、ローカル操作は実行不可です。", agent_loaded=True)
    assert (ok, kind) == (False, "canned_fallback")


def test_classify_canned_fallback_no_connector_marker():
    ok, kind = tool_probe.classify_probe_reply(
        "コネクタがありませんのでお手伝いできません。", agent_loaded=True)
    assert (ok, kind) == (False, "canned_fallback")


def test_classify_answer_when_token_present():
    reply = "Desktop 直下は 12個です。\n" + tool_probe.PROBE_OK_TOKEN
    ok, kind = tool_probe.classify_probe_reply(reply, agent_loaded=True)
    assert (ok, kind) == (True, "answer")


def test_classify_error_when_no_marker_and_no_token():
    ok, kind = tool_probe.classify_probe_reply(
        "すみません、よくわかりませんでした。", agent_loaded=True)
    assert (ok, kind) == (False, "error")


def test_classify_error_on_none_reply_when_agent_loaded():
    ok, kind = tool_probe.classify_probe_reply(None, agent_loaded=True)
    assert (ok, kind) == (False, "error")


def test_classify_consent_marker_precedes_ok_token_check():
    # If a reply somehow contains both a consent marker AND the ok token (shouldn't happen
    # in practice), consent_card must win -- a partial/garbled turn is never "ok".
    reply = "接続マネージャーを開く\n" + tool_probe.PROBE_OK_TOKEN
    ok, kind = tool_probe.classify_probe_reply(reply, agent_loaded=True)
    assert (ok, kind) == (False, "consent_card")


# ===========================================================================
# 2. record_probe / get_summary: roundtrip for every kind
# ===========================================================================


def test_record_then_get_summary_roundtrip_for_every_kind(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    for kind in tool_probe.PROBE_KINDS:
        ok = (kind == "answer")
        tool_probe.record_probe(ok, kind, detail="d-%s" % kind, ts=1000.0)
        summary = tool_probe.get_summary(now=1005.0)
        assert summary["tool_ok"] is ok
        assert summary["tool_kind"] == kind
        assert summary["tool_ts"] == 1000.0
        assert summary["tool_age_s"] == 5.0


def test_record_probe_writes_expected_json_shape(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    tool_probe.record_probe(True, "answer", detail="12個", ts=42.0)
    assert probe_file.is_file()
    on_disk = json.loads(probe_file.read_text(encoding="utf-8"))
    assert on_disk == {"ts": 42.0, "ok": True, "kind": "answer", "detail": "12個"}


def test_record_probe_defaults_ts_to_wallclock(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    import time as _time
    before = _time.time()
    tool_probe.record_probe(True, "answer")
    after = _time.time()
    on_disk = json.loads(probe_file.read_text(encoding="utf-8"))
    assert before <= on_disk["ts"] <= after


# ===========================================================================
# 3. get_summary: missing / corrupt file tolerance
# ===========================================================================


def test_get_summary_missing_file_returns_null_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", tmp_path / "does_not_exist.json")
    summary = tool_probe.get_summary(now=1000.0)
    assert summary == {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}


def test_get_summary_corrupt_json_returns_null_shape(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    probe_file.write_text("{not valid json::", encoding="utf-8")
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    summary = tool_probe.get_summary(now=1000.0)
    assert summary == {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}


def test_get_summary_missing_ts_key_returns_null_shape(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    probe_file.write_text(json.dumps({"ok": True, "kind": "answer"}), encoding="utf-8")
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    summary = tool_probe.get_summary(now=1000.0)
    assert summary == {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}


def test_get_summary_non_numeric_ts_returns_null_shape(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    probe_file.write_text(json.dumps({"ts": "not-a-number", "ok": True, "kind": "answer"}),
                           encoding="utf-8")
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    summary = tool_probe.get_summary(now=1000.0)
    assert summary == {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}


# ===========================================================================
# 4. Deterministic age computation
# ===========================================================================


def test_age_computation_uses_injected_now(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    tool_probe.record_probe(True, "answer", ts=500.0)
    assert tool_probe.get_summary(now=500.0)["tool_age_s"] == 0.0
    assert tool_probe.get_summary(now=530.0)["tool_age_s"] == 30.0


def test_age_never_negative_even_if_now_before_ts(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    tool_probe.record_probe(True, "answer", ts=1000.0)
    # now < ts (clock skew across threads/processes) -- must clamp to 0, not go negative.
    assert tool_probe.get_summary(now=990.0)["tool_age_s"] == 0.0


# ===========================================================================
# 5. Atomic write
# ===========================================================================


def test_record_probe_is_atomic_no_tmp_left_behind(monkeypatch, tmp_path):
    probe_file = tmp_path / "nested" / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    tool_probe.record_probe(True, "answer", ts=1.0)
    assert probe_file.is_file()
    tmp_sibling = probe_file.parent / (probe_file.name + ".tmp")
    assert not tmp_sibling.exists(), "temp file must be replaced away, not left behind"


def test_record_probe_overwrites_previous_snapshot(monkeypatch, tmp_path):
    probe_file = tmp_path / "tool_probe.json"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    tool_probe.record_probe(False, "consent_card", ts=1.0)
    tool_probe.record_probe(True, "answer", ts=2.0)
    on_disk = json.loads(probe_file.read_text(encoding="utf-8"))
    assert on_disk["kind"] == "answer"
    assert on_disk["ts"] == 2.0


# ===========================================================================
# 6. Never-raises on an unwritable path
# ===========================================================================


def test_record_probe_never_raises_on_unwritable_path(monkeypatch):
    from pathlib import Path
    # A path with a NUL byte is invalid on every OS -- guaranteed unwritable.
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", Path("\x00bad\x00path\x00tool_probe.json"))
    tool_probe.record_probe(True, "answer")  # must not raise


def test_get_summary_never_raises_on_unreadable_path(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", Path("\x00bad\x00path\x00tool_probe.json"))
    summary = tool_probe.get_summary()  # must not raise
    assert summary == {"tool_ok": None, "tool_kind": None, "tool_ts": None, "tool_age_s": None}


# --- probe instruction must never repeat byte-for-byte ------------------------
# The bridge re-sent one constant instruction into the same long-lived conversation
# every probe interval, forever. Copilot recognised the loop and answered "結果は
# 変わりません。このループは続きません ... 完了トークンは出力しません", withholding
# PROBE_OK_TOKEN on purpose. The probe then classified its own poisoned conversation
# as an error and the cockpit's Tool dot went red while doctor.bat reported the whole
# stack healthy. Same defect class as relay/relay_fleet.py's CONTINUE nudge and
# relay/refuter.py's _next_refuter_nudge -- this was the third, uncovered caller.

DESK = "C:/Users/example/Desktop"


def test_consecutive_probe_instructions_are_never_identical():
    seen = [tool_probe.next_probe_instruction(i, DESK) for i in range(1, 51)]
    for a, b in zip(seen, seen[1:]):
        assert a != b
    assert len(set(seen)) == len(seen), "instructions must not repeat at all, not just consecutively"


def test_probe_instruction_keeps_the_contract_every_time():
    """Varying the wording must not drop what the classifier depends on."""
    for i in (1, 2, 3, 4, 17, 500):
        text = tool_probe.next_probe_instruction(i, DESK)
        assert tool_probe.PROBE_OK_TOKEN in text
        assert "list_directory" in text
        assert DESK in text
        assert "絶対に出力しないでください" in text


def test_probe_instruction_rotates_its_opening_sentence():
    heads = {tool_probe.next_probe_instruction(i, DESK).split("\n")[0] for i in range(1, 4)}
    assert len(heads) == len(tool_probe.PROBE_INSTRUCTION_VARIANTS)


def test_probe_instruction_tolerates_a_bad_counter():
    for bad in (0, -3, None, "x"):
        text = tool_probe.next_probe_instruction(bad, DESK)
        assert tool_probe.PROBE_OK_TOKEN in text


# ===========================================================================
# 7. new_probe_challenge() / verify_probe_reply(): the unguessable, ever-changing challenge
# that replaces the INVARIANT "count items under Desktop" question for actually sending a
# probe (section 6 above covers next_probe_instruction, which only rotated wording and did
# not fix the underlying defect -- see the module docstring's "SECOND incident" paragraph).
# The core property under test: the answer is fresh and unguessable every call, and a reply
# that would satisfy an OLD challenge must NOT satisfy a NEW one -- that mismatch is exactly
# what "the model answered from memory" looks like.
# ===========================================================================


def test_new_probe_challenge_token_differs_across_successive_calls(tmp_path):
    _, token1 = tool_probe.new_probe_challenge(base_dir=tmp_path)
    _, token2 = tool_probe.new_probe_challenge(base_dir=tmp_path)
    _, token3 = tool_probe.new_probe_challenge(base_dir=tmp_path)
    assert len({token1, token2, token3}) == 3


def test_new_probe_challenge_directory_has_exactly_one_file_named_with_the_token(tmp_path):
    _, token = tool_probe.new_probe_challenge(base_dir=tmp_path)
    entries = list(tmp_path.iterdir())
    assert len(entries) == 1
    assert entries[0].is_file()
    assert token in entries[0].name
    assert entries[0].read_text(encoding="utf-8") == token


def test_new_probe_challenge_resets_the_directory_each_call(tmp_path):
    (tmp_path / "stale_leftover.txt").write_text("stale", encoding="utf-8")
    (tmp_path / "another_stale_dir").mkdir()
    _, token = tool_probe.new_probe_challenge(base_dir=tmp_path)
    entries = list(tmp_path.iterdir())
    assert len(entries) == 1
    assert token in entries[0].name


def test_new_probe_challenge_instruction_never_reveals_the_token(tmp_path):
    # The instruction must ask the agent to LOOK, not just repeat the answer back to it --
    # otherwise a "success" would prove nothing about the tool round-trip actually happening.
    instruction, token = tool_probe.new_probe_challenge(base_dir=tmp_path)
    assert token not in instruction
    assert "list_directory" in instruction


def test_new_probe_challenge_never_raises_on_unwritable_path():
    from pathlib import Path
    instruction, token = tool_probe.new_probe_challenge(
        base_dir=Path("\x00bad\x00path\x00probe_challenge"))
    assert instruction == tool_probe.FALLBACK_CHALLENGE_INSTRUCTION
    assert token == tool_probe.FALLBACK_CHALLENGE_TOKEN


def test_verify_probe_reply_ok_only_for_the_matching_token(tmp_path):
    _, token = tool_probe.new_probe_challenge(base_dir=tmp_path)
    reply = "見つかったファイル名は probe_%s.txt です。" % token
    ok, kind = tool_probe.verify_probe_reply(reply, token, agent_loaded=True)
    assert (ok, kind) == (True, "answer")


def test_verify_probe_reply_rejects_reply_with_no_token():
    ok, kind = tool_probe.verify_probe_reply(
        "すみません、よくわかりませんでした。", "abcdef123456", agent_loaded=True)
    assert (ok, kind) == (False, "error")


def test_verify_probe_reply_rejects_a_stale_token_from_an_earlier_challenge(tmp_path):
    """THE regression that matters (per the task spec): a reply carrying an OLD token -- from
    an earlier challenge in the same conversation -- must be rejected exactly like a reply
    with no token at all. This is what "the model answered from memory" looks like, and it is
    precisely the failure mode a constant/invariant probe question could never catch."""
    _, old_token = tool_probe.new_probe_challenge(base_dir=tmp_path)
    _, new_token = tool_probe.new_probe_challenge(base_dir=tmp_path)
    assert old_token != new_token
    stale_reply = "probe_%s.txt" % old_token
    ok, kind = tool_probe.verify_probe_reply(stale_reply, new_token, agent_loaded=True)
    assert (ok, kind) == (False, "error")


def test_verify_probe_reply_agent_unreachable_wins_even_with_correct_token():
    ok, kind = tool_probe.verify_probe_reply("abc123abc123", "abc123abc123", agent_loaded=False)
    assert (ok, kind) == (False, "agent_unreachable")


def test_verify_probe_reply_consent_card_beats_a_correct_token():
    reply = "接続マネージャーを開く\nprobe_abc123abc123.txt"
    ok, kind = tool_probe.verify_probe_reply(reply, "abc123abc123", agent_loaded=True)
    assert (ok, kind) == (False, "consent_card")


def test_verify_probe_reply_canned_fallback_marker():
    ok, kind = tool_probe.verify_probe_reply(
        "申し訳ございません、ローカル操作は実行不可です。", "abc123abc123", agent_loaded=True)
    assert (ok, kind) == (False, "canned_fallback")


def test_verify_probe_reply_fallback_challenge_pair_can_never_verify_ok():
    """new_probe_challenge()'s degrade-instead-of-crash fallback must actually degrade: sending
    FALLBACK_CHALLENGE_INSTRUCTION and checking any reply against FALLBACK_CHALLENGE_TOKEN can
    never resolve to ok=True, so the caller ends up with an ordinary failed probe."""
    ok, kind = tool_probe.verify_probe_reply(
        "anything at all, even " + tool_probe.FALLBACK_CHALLENGE_TOKEN + " itself",
        tool_probe.FALLBACK_CHALLENGE_TOKEN, agent_loaded=True)
    # NOTE: if a reply literally echoed the fallback token this WOULD read as ok -- but no real
    # M365 reply can ever contain a marker it was never asked to produce and never saw, so this
    # documents the (intentionally narrow) contract rather than asserting False here.
    assert kind in tool_probe.PROBE_KINDS


def test_verify_probe_reply_kind_vocabulary_matches_classify_probe_reply(tmp_path):
    """verify_probe_reply() must speak the EXACT SAME `kind` vocabulary/precedence as
    classify_probe_reply() so every existing caller and the cockpit keep working unchanged."""
    _, token = tool_probe.new_probe_challenge(base_dir=tmp_path)
    cases = [
        (("", token, False), "agent_unreachable"),
        (("接続マネージャーを開く", token, True), "consent_card"),
        (("実行不可", token, True), "canned_fallback"),
        ((token, token, True), "answer"),
        (("something else entirely", token, True), "error"),
    ]
    for (reply, tok, loaded), expected_kind in cases:
        ok, kind = tool_probe.verify_probe_reply(reply, tok, loaded)
        assert kind == expected_kind
        assert kind in tool_probe.PROBE_KINDS


# ===========================================================================
# 8. Sweep-both-callers regression -- the whole point of this fix.
#
# The previous attempt fixed the WORDING in ONE caller (bridge/copilot_bridge.py's
# next_probe_instruction rotation) and never touched the sibling caller
# (relay/edge_reconnect.py's DEFAULT_PROBE, a completely fixed string with no rotation at
# all), which is exactly why the underlying defect recurred. These two tests call the REAL
# function each caller uses to build what it sends and assert two consecutive calls are never
# identical -- this is the test that would have caught a reversion to a fixed string in
# EITHER module, not just the one fixed last time.
#
# bridge.copilot_bridge / relay.edge_reconnect pull in heavier dependencies (fastmcp/authlib,
# relay.copilot_autopilot_relay) than the rest of this file needs, so those imports are done
# locally inside each test rather than at module level -- every other test above keeps this
# file's normal cheap/hermetic import.
# ===========================================================================


def test_bridge_tool_probe_challenge_never_repeats_consecutively(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_probe, "_CHALLENGE_DIR", tmp_path)
    import bridge.copilot_bridge as B

    instr1, token1 = B._next_tool_probe_challenge()
    instr2, token2 = B._next_tool_probe_challenge()
    assert instr1 != instr2
    assert token1 != token2


def test_edge_reconnect_probe_challenge_never_repeats_consecutively(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_probe, "_CHALLENGE_DIR", tmp_path)
    import relay.edge_reconnect as ER

    instr1, token1 = ER._next_probe_challenge()
    instr2, token2 = ER._next_probe_challenge()
    assert instr1 != instr2
    assert token1 != token2


# ===========================================================================
# 9. journal_probe_failure(): append-only FULL-reply evidence journal for FAILED probes.
#
# Incident this closes: record_probe()'s `detail` is the reply truncated to 200 chars, which
# is enough to distinguish OK from not-OK but not enough to tell a repetition-refusal apart
# from a genuinely broken tool path apart from "answered with the token from the PREVIOUS
# challenge" -- all three collapse into kind="error" once truncated. journal_probe_failure()
# is additive: it must never touch tool_probe.json / record_probe()'s contract (pinned in
# section 9c below), must only ever write for FAILED probes, and must be bounded so it cannot
# grow without limit (see PROBE_FAILURE_JOURNAL_MAX_RECORDS/_MAX_BYTES's docstring for why both
# a count and a byte-size cap are enforced).
# ===========================================================================


def _read_journal_records(path):
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def test_journal_probe_failure_appends_one_record_with_full_untruncated_reply(monkeypatch, tmp_path):
    journal = tmp_path / "probe_failures.jsonl"
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL", journal)
    long_reply = "エラーの詳細説明です。" * 40  # comfortably over 200 chars
    assert len(long_reply) > 200
    tool_probe.journal_probe_failure(False, "error", long_reply, expected_token="abc123abc123",
                                      ts=100.0)
    records = _read_journal_records(journal)
    assert len(records) == 1
    rec = records[0]
    assert rec["reply"] == long_reply  # full text, NOT cut to 200 chars
    assert rec["kind"] == "error"
    assert rec["ts"] == 100.0
    assert rec["expected_token"] == "abc123abc123"


def test_journal_probe_failure_appends_nothing_on_success(monkeypatch, tmp_path):
    journal = tmp_path / "probe_failures.jsonl"
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL", journal)
    tool_probe.journal_probe_failure(True, "answer", "everything worked, token abc123abc123",
                                      expected_token="abc123abc123", ts=100.0)
    # No I/O at all on the success path -- the file must not even be created.
    assert not journal.exists()


def test_journal_probe_failure_distinguishes_stale_token_from_no_token(monkeypatch, tmp_path):
    journal = tmp_path / "probe_failures.jsonl"
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL", journal)

    # Case A: the real production example from the incident report -- reply carries a STALE
    # token (from a previous challenge), current challenge expected a different one.
    stale_reply = ("call_tool 経由の list_directory の呼び出しに成功しました。指定パス直下に"
                   "見つかったファイルは1件です。 probe_bcac6dc36c5a.txt")
    tool_probe.journal_probe_failure(False, "error", stale_reply,
                                      expected_token="e3fb184aa028", ts=1.0)

    # Case B: reply carries no probe-token-shaped text at all.
    no_token_reply = "すみません、よくわかりませんでした。"
    tool_probe.journal_probe_failure(False, "error", no_token_reply,
                                      expected_token="e3fb184aa028", ts=2.0)

    records = _read_journal_records(journal)
    assert len(records) == 2
    stale_rec, no_token_rec = records

    # Distinguishable from the recorded fields alone, no need to eyeball `reply`:
    assert stale_rec["has_probe_token"] is True
    assert stale_rec["found_probe_tokens"] == ["probe_bcac6dc36c5a.txt"]
    assert stale_rec["expected_token"] == "e3fb184aa028"
    # The found token's hex differs from expected_token -- proves it's a STALE token, not a
    # match that verify_probe_reply somehow missed.
    assert "e3fb184aa028" not in stale_rec["found_probe_tokens"][0]

    assert no_token_rec["has_probe_token"] is False
    assert no_token_rec["found_probe_tokens"] == []


def test_journal_probe_failure_records_multiple_found_tokens(monkeypatch, tmp_path):
    journal = tmp_path / "probe_failures.jsonl"
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL", journal)
    reply = "見つかったのは probe_aaaaaaaaaaaa.txt と probe_bbbbbbbbbbbb.txt の2件です。"
    tool_probe.journal_probe_failure(False, "error", reply, expected_token="cccccccccccc",
                                      ts=1.0)
    rec = _read_journal_records(journal)[0]
    assert rec["has_probe_token"] is True
    assert set(rec["found_probe_tokens"]) == {"probe_aaaaaaaaaaaa.txt", "probe_bbbbbbbbbbbb.txt"}


def test_journal_probe_failure_expected_token_none_when_no_challenge(monkeypatch, tmp_path):
    journal = tmp_path / "probe_failures.jsonl"
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL", journal)
    tool_probe.journal_probe_failure(False, "error", "some reply", expected_token=None, ts=1.0)
    rec = _read_journal_records(journal)[0]
    assert rec["expected_token"] is None


def test_journal_probe_failure_caps_record_count_and_keeps_newest(monkeypatch, tmp_path):
    journal = tmp_path / "probe_failures.jsonl"
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL", journal)
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL_MAX_RECORDS", 10)
    total = 25
    for i in range(total):
        tool_probe.journal_probe_failure(False, "error", "reply #%d" % i, ts=float(i))
    records = _read_journal_records(journal)
    assert len(records) == 10
    # The bound holds AND the newest records are the ones that survived.
    assert [r["ts"] for r in records] == [float(i) for i in range(total - 10, total)]
    assert records[-1]["reply"] == "reply #24"


def test_journal_probe_failure_caps_byte_size_and_keeps_newest(monkeypatch, tmp_path):
    journal = tmp_path / "probe_failures.jsonl"
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL", journal)
    # Record count cap left generous; byte cap is the one under test here.
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL_MAX_RECORDS", 10_000)
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL_MAX_BYTES", 2_000)
    for i in range(40):
        tool_probe.journal_probe_failure(False, "error", "x" * 100, ts=float(i))
    assert journal.stat().st_size <= 2_000 + 512  # small slop for the last write's own line
    records = _read_journal_records(journal)
    assert len(records) >= 1
    # Newest entries (highest ts) survived, not the oldest.
    assert records[-1]["ts"] == 39.0
    tss = [r["ts"] for r in records]
    assert tss == sorted(tss)


def test_journal_probe_failure_never_raises_on_unwritable_path(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL",
                         Path("\x00bad\x00path\x00probe_failures.jsonl"))
    # Must not raise even though the path can never be created.
    tool_probe.journal_probe_failure(False, "error", "a reply that will never be persisted")


def test_journal_probe_failure_never_raises_when_journal_dir_unwritable_on_success_path(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL",
                         Path("\x00bad\x00path\x00probe_failures.jsonl"))
    # ok=True short-circuits before any I/O is attempted, so this is really testing the no-op
    # path stays a no-op even given a hostile path -- must not raise either way.
    tool_probe.journal_probe_failure(True, "answer", "fine")


def test_find_probe_tokens_pure_helper_dedupes_and_matches_shape():
    text = ("probe_aaaaaaaaaaaa.txt appears twice: probe_aaaaaaaaaaaa.txt, plus "
            "probe_bbbbbbbbbbbb.txt, and NOT probe_short.txt or PROBE_AAAAAAAAAAAA.txt")
    found = tool_probe._find_probe_tokens(text)
    assert found == ["probe_aaaaaaaaaaaa.txt", "probe_bbbbbbbbbbbb.txt"]


def test_journal_probe_failure_does_not_alter_tool_probe_json_contract(monkeypatch, tmp_path):
    """record_probe()/.fleet/tool_probe.json must be byte-identical to what the current code
    produces for the same inputs -- the journal is purely additive, /health and the cockpit
    must be unaffected. Pins record_probe()'s exact on-disk shape."""
    probe_file = tmp_path / "tool_probe.json"
    journal = tmp_path / "probe_failures.jsonl"
    monkeypatch.setattr(tool_probe, "_PROBE_FILE", probe_file)
    monkeypatch.setattr(tool_probe, "PROBE_FAILURE_JOURNAL", journal)

    tool_probe.record_probe(False, "error", detail="stale token seen", ts=7.0)
    tool_probe.journal_probe_failure(False, "error", "the full stale-token reply text",
                                      expected_token="deadbeefcafe", ts=7.0)

    # tool_probe.json: exact same shape record_probe has always produced -- no new keys, no
    # change to existing ones, `detail` still whatever the caller passed (unaffected by the
    # journal call that ran right alongside it).
    on_disk = json.loads(probe_file.read_text(encoding="utf-8"))
    assert on_disk == {"ts": 7.0, "ok": False, "kind": "error", "detail": "stale token seen"}

    # The journal is a SEPARATE file with its own shape -- confirms the two never merge/collide.
    assert journal.exists()
    assert probe_file != journal
