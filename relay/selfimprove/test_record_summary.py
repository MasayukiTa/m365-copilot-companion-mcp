"""Summaries are derived, bilingual, and can never become the record.

The dashboard's headline is computed from the record and needs no model. The `reason` cannot
be: it is free prose the agent typed in whichever language it was working in, which is why
toggling the interface to English left those lines in Japanese -- they are not interface text.
Shortening and translating one genuinely needs a model; that is language conversion, not a
lookup table dressed up as intelligence.

What these tests protect is the boundary. The ledger is append-only and its reason is what was
actually typed; a summary that replaced it would put an unverifiable paraphrase where a record
used to be. So: separate cache, keyed by the record's own hash, and every failure path lands
on the raw reason -- which is what the screen showed before any of this existed.

Run: pytest -q relay/selfimprove/test_record_summary.py
"""
import json

import pytest

import relay.selfimprove.record_summary as RS

LONG_JA = ("tools/security.py:279 の no-HTTP-context 拒否メッセージが実行不可能な操作を指示していた。"
           "unlock() も同じ HTTP コンテキストを必要とするため、従った読み手は同じ理由で失敗する。")
LONG_EN = ("route_evaluator.preflight now also refuses below the fleet's disk floor, reading "
           "relay_fleet.DEFAULT_DISK_FLOOR_GB rather than choosing its own value.")


def _rec(h, reason, event="rebless", changed=None):
    return {"hash": h, "reason": reason, "event": event,
            "changed": changed or {"tools/security.py": "x"}}


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(RS, "CACHE_PATH", str(tmp_path / "summaries.json"))
    return tmp_path / "summaries.json"


# ── the boundary ────────────────────────────────────────────────────────────────────

def test_a_summary_is_keyed_by_the_records_own_hash():
    """Nothing else identifies a record -- seq numbers repeat across ledgers -- and the key is
    what makes attaching a summary to the wrong record structurally impossible."""
    assert RS.key_of(_rec("abc123", LONG_EN)) == "abc123"
    assert RS.key_of({"reason": "x"}) == ""


def test_an_edited_record_simply_misses_its_summary():
    RS.save({"abc123": {"ja": "要約", "en": "summary"}})
    assert RS.summary_for(_rec("abc123", LONG_EN), "ja") == "要約"
    assert RS.summary_for(_rec("abc124", LONG_EN), "ja") == ""


def test_no_summary_is_an_empty_string_and_the_caller_falls_back():
    assert RS.summary_for(_rec("nope", LONG_EN), "ja") == ""
    assert RS.summary_for(_rec("nope", LONG_EN), "en") == ""


def test_deleting_the_cache_returns_the_screen_to_what_it_showed_before(cache):
    RS.save({"abc123": {"ja": "要約", "en": "summary"}})
    cache.unlink()
    assert RS.summary_for(_rec("abc123", LONG_EN), "ja") == ""


def test_an_unknown_language_gets_nothing_rather_than_a_guess():
    RS.save({"abc123": {"ja": "要約", "en": "summary"}})
    assert RS.summary_for(_rec("abc123", LONG_EN), "fr") == ""


# ── what is worth summarising ───────────────────────────────────────────────────────

def test_a_short_reason_is_left_alone():
    """Replacing a 30-character sentence with a 30-character summary spends a model call to
    change nothing."""
    assert RS.missing([_rec("a", "swapped with active_manifest.json.prev")]) == []


def test_a_long_reason_in_either_language_is_queued():
    got = RS.missing([_rec("a", LONG_EN), _rec("b", LONG_JA)])
    assert sorted(RS.key_of(r) for r in got) == ["a", "b"]


def test_a_record_with_both_languages_is_not_queued_again():
    RS.save({"a": {"ja": "あ", "en": "b"}})
    assert RS.missing([_rec("a", LONG_EN)]) == []


def test_a_half_filled_entry_is_still_missing():
    """Half a bilingual summary is the language bug again, for that record."""
    RS.save({"a": {"ja": "あ", "en": ""}})
    assert [RS.key_of(r) for r in RS.missing([_rec("a", LONG_EN)])] == ["a"]


def test_a_record_without_a_reason_is_not_queued():
    assert RS.missing([_rec("a", "")]) == []


# ── reading a model's reply ─────────────────────────────────────────────────────────

def test_a_well_formed_reply_is_accepted():
    got = RS.parse_reply('{"ja": "拒否文を実行可能な文面に修正", "en": "reword the refusal"}')
    assert got["ja"] == "拒否文を実行可能な文面に修正"
    assert got["en"] == "reword the refusal"
    assert got["trimmed"] is False


def test_a_fenced_reply_is_accepted():
    got = RS.parse_reply('ここです:\n```json\n{"ja": "あ", "en": "b"}\n```\n以上')
    assert got["ja"] == "あ" and got["en"] == "b"


def test_a_reply_that_did_not_answer_is_refused_rather_than_massaged():
    """The alternative to a summary is the raw reason, which is a fine outcome. Guessing at
    malformed output is how a wrong summary gets stored."""
    for bad in ("すみません、要約できません", "", "{}", '{"ja": "あ"}', '{"en": "b"}', "not json"):
        assert RS.parse_reply(bad) == {}, bad


def test_an_over_long_summary_is_trimmed_not_rejected():
    got = RS.parse_reply(json.dumps({"ja": "あ" * 200, "en": "b" * 400}))
    assert len(got["ja"]) <= RS.MAX_JA + 1      # +1 for the ellipsis
    assert len(got["en"]) <= RS.MAX_EN + 1


def test_a_trimmed_summary_says_it_was_trimmed():
    """The first pass cut at exactly the limit, and on screen the result read as a sentence
    that broke rather than one that was shortened -- a reader could not tell whether the
    summary or the record was at fault."""
    got = RS.parse_reply(json.dumps({"ja": "あ" * 200, "en": "word " * 200}))
    assert got["ja"].endswith("…")
    assert got["en"].endswith("…")


def test_a_trim_prefers_a_sentence_break():
    long_ja = "最初の文です。" + "あ" * 200
    got = RS.parse_reply(json.dumps({"ja": long_ja, "en": "x" * 300}))
    assert got["ja"].startswith("最初の文です。")


def test_a_summary_that_fits_is_left_exactly_as_written():
    got = RS.parse_reply(json.dumps({"ja": "短い要約", "en": "short summary"}))
    assert got["ja"] == "短い要約" and got["en"] == "short summary"
    assert got["trimmed"] is False


# ── generating ──────────────────────────────────────────────────────────────────────

def test_the_prompt_forbids_adding_anything_not_in_the_record():
    p = RS.build_prompt(_rec("a", LONG_EN))
    assert "記録に書かれていないことを足さない" in p
    assert "評価や推測を書かない" in p
    assert LONG_EN in p


def test_the_prompt_carries_the_computed_facts_so_it_cannot_contradict_them():
    p = RS.build_prompt(_rec("a", LONG_EN, event="rebless"))
    assert "rebless" in p and "tools/security.py" in p


def test_backfill_stores_both_languages_and_reports():
    rep = RS.backfill([_rec("a", LONG_EN)],
                      lambda prompt: '{"ja": "あ", "en": "b"}', model="test")
    assert rep == {"generated": 1, "failed": 0, "remaining": 0}
    assert RS.summary_for(_rec("a", LONG_EN), "ja") == "あ"
    assert RS.summary_for(_rec("a", LONG_EN), "en") == "b"


def test_backfill_logs_the_source_beside_the_summary():
    """The only real check on an extractive summary is a person reading both."""
    lines = []
    RS.backfill([_rec("a", LONG_EN)], lambda p: '{"ja": "あ", "en": "b"}',
                log=lines.append)
    joined = "\n".join(lines)
    assert "source" in joined and LONG_EN[:40] in joined
    assert "ja" in joined and "en" in joined


def test_a_failing_model_call_leaves_the_cache_alone():
    def boom(prompt):
        raise RuntimeError("no browser")
    rep = RS.backfill([_rec("a", LONG_EN)], boom)
    assert rep["generated"] == 0 and rep["failed"] == 1
    assert RS.summary_for(_rec("a", LONG_EN), "ja") == ""


def test_an_unusable_reply_is_counted_and_not_stored():
    rep = RS.backfill([_rec("a", LONG_EN)], lambda p: "I cannot do that")
    assert rep == {"generated": 0, "failed": 1, "remaining": 1}
    assert RS.summary_for(_rec("a", LONG_EN), "ja") == ""


def test_the_limit_is_respected():
    rep = RS.backfill([_rec("a", LONG_EN), _rec("b", LONG_JA)],
                      lambda p: '{"ja": "あ", "en": "b"}', limit=1)
    assert rep["generated"] == 1 and rep["remaining"] == 1


def test_backfill_is_idempotent():
    calls = []

    def ask(p):
        calls.append(p)
        return '{"ja": "あ", "en": "b"}'

    RS.backfill([_rec("a", LONG_EN)], ask)
    RS.backfill([_rec("a", LONG_EN)], ask)
    assert len(calls) == 1


def test_the_cache_is_not_published():
    import subprocess
    real = RS.__dict__["_REPO"]
    import os
    path = os.path.join(real, ".fleet", "selfimprove", "record_summaries.json")
    r = subprocess.run(["git", "check-ignore", "-q", path])
    assert r.returncode == 0, path


def test_the_module_states_that_the_record_is_never_rewritten():
    import io as _io
    src = _io.open(RS.__file__, encoding="utf-8").read()
    assert "THE RECORD IS NEVER REWRITTEN" in src


# ── a diagnostic must not be able to destroy the work ───────────────────────────────

def test_a_log_that_throws_does_not_kill_the_backfill():
    """Measured: a model reply carried an em dash, the console was cp932, print raised
    UnicodeEncodeError, and the whole run died after generating most of the records and
    before anything was saved. It exited 0 because it sat behind a pipe."""
    def hostile(_m):
        raise UnicodeEncodeError("cp932", "x", 0, 1, "illegal multibyte sequence")

    rep = RS.backfill([_rec("a", LONG_EN), _rec("b", LONG_JA)],
                      lambda p: '{"ja": "あ", "en": "b"}', log=hostile)
    assert rep["generated"] == 2


def test_each_summary_is_saved_as_it_is_generated():
    """Saving only at the end meant one exception discarded every summary before it."""
    seen = []

    def ask(prompt):
        seen.append(RS.load())
        return '{"ja": "あ", "en": "b"}'

    RS.backfill([_rec("a", LONG_EN), _rec("b", LONG_JA)], ask)
    assert seen[1], "the first summary must already be on disk when the second call is made"


def test_a_crash_part_way_keeps_what_was_already_generated():
    calls = {"n": 0}

    def ask(prompt):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("browser died")
        return '{"ja": "あ", "en": "b"}'

    RS.backfill([_rec("a", LONG_EN), _rec("b", LONG_JA)], ask)
    assert RS.summary_for(_rec("a", LONG_EN), "ja") == "あ"


# ── a summary that had to be cut is not a summary the model produced ────────────────

def test_a_fit_is_judged_by_the_truncation_mark_not_the_length():
    """parse_reply has already trimmed by the time anyone can look, so a length test would
    call every reply a fit."""
    assert RS._fits({"ja": "短い", "en": "short"}) is True
    assert RS._fits(RS.parse_reply(json.dumps({"ja": "あ" * 300, "en": "b" * 400}))) is False
    assert RS._fits({}) is False


def test_an_overrunning_reply_is_asked_once_more_before_being_accepted():
    """The first prompt asked for "a one-line summary" and got the record's opening back:
    27 of 28 hit the cap and were shown truncated -- an honest truncation of something that
    had not been summarised."""
    asked = []

    def ask(prompt):
        asked.append(prompt)
        if len(asked) == 1:
            return json.dumps({"ja": "あ" * 300, "en": "b" * 400})
        return json.dumps({"ja": "短い要約", "en": "short summary"})

    RS.backfill([_rec("a", LONG_EN)], ask)
    assert len(asked) == 2
    assert "長すぎました" in asked[1]
    assert RS.summary_for(_rec("a", LONG_EN), "ja") == "短い要約"


def test_the_retry_happens_once_and_the_result_is_kept_either_way():
    """One retry, not a loop: another turn is cheap, never converging is a backfill that
    never ends. A second overflow is stored truncated, with the ellipsis saying so."""
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return json.dumps({"ja": "あ" * 300, "en": "b" * 400})

    rep = RS.backfill([_rec("a", LONG_EN)], ask)
    assert len(calls) == 2
    assert rep["generated"] == 1
    assert RS.summary_for(_rec("a", LONG_EN), "ja").endswith(RS.TRUNCATED)


def test_the_prompt_tells_the_model_not_to_copy_the_opening():
    p = RS.build_prompt(_rec("a", LONG_EN))
    assert "冒頭の抜き書きではありません" in p
    assert "先頭の文をそのまま写さない" in p
    assert "例:" in p          # an example is worth more than another adjective


# ── the log must not kill the run, at EVERY layer ───────────────────────────────────

def test_the_socket_route_gets_the_safe_printer_too():
    """The cp932 fix reached `backfill` and stopped there. The route is handed a printer as
    well, and its failure notes carry model and exception text -- so the same class was still
    live one call away."""
    import inspect
    src = inspect.getsource(RS._copilot_asker)
    assert "log=safe_print" in src
    assert "print(m, flush=True)" not in src


def test_the_safe_printer_survives_a_console_that_cannot_hold_the_text(monkeypatch):
    written = []

    class Narrow:
        encoding = "cp932"

        def write(self, text):
            text.encode("cp932")          # raises on an em dash, like the real console
            written.append(text)

        def flush(self):
            pass

    monkeypatch.setattr(RS.sys, "stdout", Narrow())
    RS.safe_print("an em dash — here")      # must not raise
    assert written and "here" in written[-1]


def test_one_printer_rather_than_two_spellings_of_the_rule():
    import inspect
    src = inspect.getsource(RS)
    assert src.count("def safe_print") == 1
    assert "log=safe_print" in src


def test_a_save_that_never_lands_is_reported(monkeypatch):
    """"generated: 28" while nothing reached the disk is the same self-report-versus-reality
    gap this module was already bitten by."""
    monkeypatch.setattr(RS, "save", lambda cache: (_ for _ in ()).throw(OSError("read only")))
    rep = RS.backfill([_rec("a", LONG_EN)], lambda p: '{"ja": "あ", "en": "b"}')
    assert rep.get("unsaved") == 1


def test_a_normal_run_does_not_carry_the_unsaved_key():
    rep = RS.backfill([_rec("a", LONG_EN)], lambda p: '{"ja": "あ", "en": "b"}')
    assert "unsaved" not in rep


def test_a_model_that_ends_with_an_ellipsis_is_not_called_truncated():
    """Reading the mark back off the end mistakes a model that chose to finish a sentence that
    way for one that overran, and spends a retry proving it."""
    got = RS.parse_reply(json.dumps({"ja": "続きは記録に…", "en": "the rest is in the record…"}))
    assert got["trimmed"] is False
    assert RS._fits(got) is True


def test_an_actual_overrun_is_still_caught():
    got = RS.parse_reply(json.dumps({"ja": "あ" * 300, "en": "b" * 400}))
    assert got["trimmed"] is True
    assert RS._fits(got) is False
