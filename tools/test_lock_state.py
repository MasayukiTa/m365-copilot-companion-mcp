"""A lock refusal must be detectable without relying on how the agent phrased it.

The incident: the relay decides whether to auto-unlock by looking for the server's
literal error ("[locked client IP: ...]") in the agent's reply. The operator
discipline injected into every turn tells the agent to write "淡々と事実とタスク
結果のみ", so it summarises instead -- "unlock パスワード欠如で確定。STUCK: unlock
パスワード未提供。" -- and no marker appears. Detection missed, the generic retry
nudge ran in place of the unlock injection, and the run STUCKed asking a human for
a password that was already in .env.

Run: pytest -q tools/test_lock_state.py
"""
from __future__ import annotations

import json

import pytest

from tools import lock_state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_state, "_STATE_FILE", tmp_path / "lock_state.json")
    yield


def test_no_record_means_not_locked():
    assert lock_state.read_state() == {}
    assert lock_state.locked_recently() is False


def test_a_refusal_is_visible_immediately():
    lock_state.record_locked("203.0.113.7", "[locked client IP: '203.0.113.7'] ...")
    assert lock_state.locked_recently() is True
    assert lock_state.read_state()["client_ip"] == "203.0.113.7"


def test_an_old_refusal_does_not_colour_a_later_turn():
    """Freshness is what keeps the fallback honest."""
    lock_state.record_locked("203.0.113.7", ts=1_000.0)
    assert lock_state.locked_recently(within_sec=180.0, now=1_100.0) is True
    assert lock_state.locked_recently(within_sec=180.0, now=1_500.0) is False


def test_unlock_clears_the_record():
    lock_state.record_locked("203.0.113.7")
    lock_state.clear()
    assert lock_state.locked_recently() is False


def test_corrupt_or_unreadable_state_is_treated_as_no_lock():
    lock_state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_state._STATE_FILE.write_text("{not json", encoding="utf-8")
    assert lock_state.read_state() == {}
    assert lock_state.locked_recently() is False


def test_a_record_without_a_usable_timestamp_is_not_a_lock():
    lock_state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    for bad in ({"ts": "later"}, {"ts": 0}, {}):
        lock_state._STATE_FILE.write_text(json.dumps(bad), encoding="utf-8")
        assert lock_state.locked_recently() is False


def test_detail_and_ip_are_bounded():
    lock_state.record_locked("x" * 500, "y" * 5000)
    state = lock_state.read_state()
    assert len(state["client_ip"]) <= 64
    assert len(state["detail"]) <= 200


def test_recording_never_raises_even_when_the_path_is_unusable(tmp_path, monkeypatch):
    """Request handling must never break because this sidecar could not be written."""
    monkeypatch.setattr(lock_state, "_STATE_FILE", tmp_path / "nope" / "\0bad" / "s.json")
    lock_state.record_locked("203.0.113.7")      # must not raise
    lock_state.clear()                            # must not raise


# --- turn scoping ------------------------------------------------------------
# CI caught the reason this matters. tools/security.py records a refusal into the
# real sidecar, so one test calling require_unlocked() left a fresh record behind;
# a later test's ordinary refusal reply ("I cannot assist with that request") was
# then read as a lock, and two unrelated resilience tests failed. "Was anything
# refused lately" is too broad to judge one turn by.


def test_only_a_refusal_from_this_turn_counts():
    sent_at = 1_000.0
    lock_state.record_locked("203.0.113.7", ts=sent_at + 1)
    assert lock_state.locked_since(sent_at, now=sent_at + 2) is True


def test_a_refusal_from_before_the_turn_is_ignored():
    """The exact CI contamination: an earlier, unrelated call left the record."""
    sent_at = 1_000.0
    lock_state.record_locked("203.0.113.7", ts=sent_at - 5)
    assert lock_state.locked_since(sent_at, now=sent_at + 2) is False
    # ...while the broad query would still have said yes, which is the bug.
    assert lock_state.locked_recently(within_sec=180.0, now=sent_at + 2) is True


def test_no_boundary_means_no_detection():
    """A caller that cannot say when its turn started gets nothing, not everything."""
    lock_state.record_locked("203.0.113.7")
    for bad in (0.0, -1.0, None, "x"):
        assert lock_state.locked_since(bad) is False


def test_freshness_still_bounds_a_scoped_query():
    sent_at = 1_000.0
    lock_state.record_locked("203.0.113.7", ts=sent_at + 1)
    assert lock_state.locked_since(sent_at, now=sent_at + 10_000) is False


# ---- 追記型の拒否ログ（2026-08-21、再構成できなかった事故より） -------------------------------
#
# 単一スロットで足りていたのは1ワーカーずつ動かしていた間だけ。並列6では、誰かの拒否が
# 他の全員の回答を freshness の間ロック扱いにし、しかもファイルは「誰の拒否か」を言えなかった
# -- client_ip が空の記録に至っては書いた場所すら分からなかった。

def _log_lines(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    p = Path(str(tmp_path / "lock_refusals.jsonl"))
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture(autouse=True)
def _never_write_the_real_records(tmp_path, monkeypatch):
    """どのテストも本番の記録に触らせない。

    触れていた。追記ログを足した瞬間、このファイルのテストが 117 行を
    .fleet/lock_refusals.jsonl に書き込み、実運用の拒否履歴に
    `site=test_lock_state.py:34` が並んだ。今日これと同じ過ちを
    socket_route.jsonl で一度直している -- 新しい追記型の記録は、
    最初からテスト隔離を持たせないと必ずこうなる。
    """
    from pathlib import Path
    monkeypatch.setattr(lock_state, "_LOG_FILE", Path(str(tmp_path / "refusals.jsonl")))
    monkeypatch.setattr(lock_state, "_STATE_FILE", Path(str(tmp_path / "state.json")))


def test_no_test_in_this_file_can_reach_the_real_records():
    """上の fixture が効かなくなっても他のテストは緑のままなので、これが唯一の警報。"""
    assert ".fleet" not in str(lock_state._LOG_FILE).replace("\\", "/")
    assert ".fleet" not in str(lock_state._STATE_FILE).replace("\\", "/")


def _redirect(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(lock_state, "_LOG_FILE", Path(str(tmp_path / "lock_refusals.jsonl")))
    monkeypatch.setattr(lock_state, "_STATE_FILE", Path(str(tmp_path / "lock_state.json")))


def test_every_refusal_is_kept_not_just_the_last(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    lock_state.record_locked("203.0.113.7", "first", ts=100.0)
    lock_state.record_locked("198.51.100.9", "second", ts=101.0)
    rows = _log_lines(tmp_path, monkeypatch)
    assert [r["client_ip"] for r in rows] == ["203.0.113.7", "198.51.100.9"]
    # スロットは従来どおり最新1件。両方あることに意味がある。
    assert lock_state.read_state()["client_ip"] == "198.51.100.9"


def test_a_refusal_records_where_it_came_from(tmp_path, monkeypatch):
    """client_ip が空の記録を誰が書いているか -- これが分からず調査が止まった。
    ツール名は取れない（呼び出し元が凍結モジュール）ので、取れるものを取る。"""
    _redirect(tmp_path, monkeypatch)
    lock_state.record_locked("", "[locked: no HTTP request context]")
    site = _log_lines(tmp_path, monkeypatch)[-1]["site"]
    assert "test_lock_state.py" in site and ":" in site


def test_a_classification_names_its_branch_and_its_evidence(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    lock_state.record_locked("203.0.113.7", "refused", ts=100.0)
    lock_state.record_classification("fallback", resp_len=533, since=99.0,
                                     consumed=lock_state.read_state())
    row = _log_lines(tmp_path, monkeypatch)[-1]
    assert row["event"] == "classified_locked"
    assert row["branch"] == "fallback" and row["resp_len"] == 533
    assert row["consumed"]["client_ip"] == "203.0.113.7"


def test_matching_record_returns_what_locked_since_agreed_to(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    lock_state.record_locked("203.0.113.7", "refused", ts=100.0)
    assert lock_state.matching_record(99.0, now=101.0)["client_ip"] == "203.0.113.7"
    assert lock_state.matching_record(200.0, now=201.0) == {}


def test_a_log_that_cannot_be_written_does_not_refuse_a_call(tmp_path, monkeypatch):
    """記録できないことで、リクエスト処理が止まってはいけない。"""
    from pathlib import Path
    monkeypatch.setattr(lock_state, "_LOG_FILE", Path("\0/impossible/x.jsonl"))
    monkeypatch.setattr(lock_state, "_STATE_FILE", Path(str(tmp_path / "s.json")))
    lock_state.record_locked("203.0.113.7", "refused")      # 例外を出さないこと自体が要件
    assert lock_state.read_state()["client_ip"] == "203.0.113.7"


# ── 2026-09-09: a diagnostic that survives detail's 200-char truncation ────────────────────

def test_presented_digest_and_tokens_held_survive_a_long_detail(tmp_path, monkeypatch):
    """The whole point: appending diagnostic text to the end of `detail` never worked -- it
    is truncated to 200 chars and the fixed boilerplate sentence already uses most of them.
    These two new fields must land in the record independent of `detail`'s length."""
    _redirect(tmp_path, monkeypatch)
    long_detail = "x" * 250
    lock_state.record_locked("203.0.113.7", long_detail,
                             presented_digest="ab12cd34ef56ab78", tokens_held=42)
    row = _log_lines(tmp_path, monkeypatch)[-1]
    assert len(row["detail"]) == 200, "detail truncation itself must be unchanged"
    assert row["presented_digest"] == "ab12cd34ef56ab78"
    assert row["tokens_held"] == 42


def test_omitting_the_new_fields_keeps_the_old_callers_unaffected(tmp_path, monkeypatch):
    """The three existing call sites in tools/security.py pass neither kwarg. Backward
    compatibility means the payload shape they produce must not change."""
    _redirect(tmp_path, monkeypatch)
    lock_state.record_locked("203.0.113.7", "refused")
    row = _log_lines(tmp_path, monkeypatch)[-1]
    assert "presented_digest" not in row
    assert "tokens_held" not in row


def test_an_empty_presented_digest_is_still_recorded_distinctly(tmp_path, monkeypatch):
    """"" (nothing presented) must be distinguishable from the field being absent -- a caller
    that never attached a token needs to read differently from a caller this fix predates."""
    _redirect(tmp_path, monkeypatch)
    lock_state.record_locked("203.0.113.7", "refused", presented_digest="", tokens_held=3)
    row = _log_lines(tmp_path, monkeypatch)[-1]
    assert "presented_digest" in row and row["presented_digest"] == ""
    assert row["tokens_held"] == 3


# -- which session the refusal arrived on -------------------------------------------------------
#
# This ledger recorded client_ip and, since 2026-09-09, session_state -- but never WHICH
# session, so a refusal could not be joined to the unlock that preceded it. Measured 2026-09-10
# over three days: of 759 refusals, 595 precede any successful unlock in the same session (the
# designed first-call refusal) and 164 FOLLOW one, p50 461s / p90 1359s / max 2710s after it.
# The max exceeds the 30-minute session TTL and is explained; the median is well inside it and
# is not. Joining the two needs the session id in the same row as the ip.

def _rows(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_a_refusal_records_the_session_it_arrived_on(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_state, "_LOG_FILE", tmp_path / "refusals.jsonl")
    monkeypatch.setattr(lock_state, "_session", lambda: "abc123def456")
    lock_state.record_locked("20.0.0.1", "[locked client IP: '20.0.0.1'] ...")
    rows = [r for r in _rows(tmp_path / "refusals.jsonl") if r.get("event") == "refused"]
    assert len(rows) == 1
    assert rows[0]["session"] == "abc123def456"
    assert rows[0]["client_ip"] == "20.0.0.1", (
        "the join needs BOTH halves in one row -- the ip is the key the authorization is "
        "recorded under, the session is what may have outlived it")


def test_no_session_means_no_field_rather_than_an_empty_one(tmp_path, monkeypatch):
    """Outside an HTTP request there is no session to name. An empty string would read, to a
    later reader counting rows by session, as a session whose id happens to be blank -- which
    is exactly how the earlier blank-ip records made an incident unreconstructable."""
    monkeypatch.setattr(lock_state, "_LOG_FILE", tmp_path / "refusals.jsonl")
    monkeypatch.setattr(lock_state, "_session", lambda: "")
    lock_state.record_locked("", "[locked: no HTTP request context] ...")
    rows = [r for r in _rows(tmp_path / "refusals.jsonl") if r.get("event") == "refused"]
    assert "session" not in rows[0], rows[0]


def test_a_session_lookup_that_raises_cannot_refuse_the_call(tmp_path, monkeypatch):
    """A diagnostic that can fail a request is worse than no diagnostic."""
    monkeypatch.setattr(lock_state, "_LOG_FILE", tmp_path / "refusals.jsonl")

    def boom():
        raise RuntimeError("no context")
    monkeypatch.setattr(lock_state, "_session", boom)
    with pytest.raises(RuntimeError):
        boom()
    lock_state.record_locked("20.0.0.1", "detail")     # must not raise
