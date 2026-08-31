"""The judgement layer, as the execution path actually reaches it.

command_judge is tested as a pure policy; this checks the wiring: that shadow records without
blocking, that enforce refuses, that a read-only command never reaches a judge, and that a
broken judge cannot break execution.
"""
import io
import json
import os

import pytest

os.environ.setdefault("MCP_API_KEY", "test-key-not-used")

from tools import code_exec as CE
from tools import command_triage as T


@pytest.fixture
def judged(tmp_path, monkeypatch):
    """Capture what would be recorded, without writing to the repository's own log.

    **kwargs ON PURPOSE. This stub is a copy of _record_judgement's signature, and when the
    real function gained a `human` argument the stub did not: every call then raised TypeError,
    _judged's catch-all swallowed it, and four tests reported "nothing was recorded" rather than
    "the stub is out of date". A stub that has to be kept in step with the code is a second
    place for the code to be wrong.
    """
    rows = []
    monkeypatch.setattr(CE, "_record_judgement",
                        lambda kind, text, req, verdict, mode_name, blocks, **kw:
                        rows.append((kind, text, verdict, mode_name, blocks, kw)))
    return rows


def test_the_capture_stub_matches_the_real_recorder():
    """Otherwise the fixture silently stops observing, which is how the four failures above
    presented: as an empty list, not as a signature error."""
    import inspect
    params = list(inspect.signature(CE._record_judgement).parameters)
    assert params[:6] == ["kind", "text", "req", "verdict", "mode_name", "blocks"]


def _backend(answer):
    return lambda _req: answer


def test_shadow_records_and_does_not_block(judged, monkeypatch):
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend",
                        lambda: _backend('{"decision":"BLOCK_AND_RETRY","reason":"no"}'))
    assert CE._judged("shell", "rm -rf build", None) is None
    assert judged and judged[0][2]["decision"] == "BLOCK_AND_RETRY"
    assert judged[0][4] is True, "the record must say it WOULD have blocked"


def test_enforce_refuses_and_says_why(judged, monkeypatch):
    monkeypatch.setenv(T.MODE_ENV, "enforce")
    monkeypatch.setattr(CE, "_judge_backend",
                        lambda: _backend('{"decision":"BLOCK_AND_RETRY","reason":"deletes src"}'))
    out = CE._judged("shell", "rm -rf src", None)
    assert out is not None
    assert "deletes src" in out, out


def test_enforce_allows_what_the_judge_allows(judged, monkeypatch):
    monkeypatch.setenv(T.MODE_ENV, "enforce")
    monkeypatch.setattr(CE, "_judge_backend",
                        lambda: _backend('{"decision":"ALLOW","reason":"asked for"}'))
    assert CE._judged("shell", "rm -rf build", None) is None


def test_a_read_only_command_never_reaches_the_judge(judged, monkeypatch):
    """The latency answer. A judging call in front of `git status` buys nothing, and a layer
    that makes every trivial command slow is one people switch off."""
    monkeypatch.setenv(T.MODE_ENV, "enforce")
    called = []
    monkeypatch.setattr(CE, "_judge_backend",
                        lambda: (called.append(1), _backend('{"decision":"ALLOW"}'))[1])
    assert CE._judged("shell", "git status", None) is None
    assert not judged, "a read-only command was judged"


def test_python_source_is_always_judged(judged, monkeypatch):
    """run_python executes anything; there is no read-only spelling of it."""
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend", lambda: _backend('{"decision":"ALLOW"}'))
    CE._judged("python", "print(1)", None)
    assert judged and judged[0][0] == "python"


def test_no_backend_is_require_human_not_allow(judged, monkeypatch):
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend", lambda: None)
    CE._judged("shell", "rm -rf x", None)
    assert judged[0][2]["decision"] == "REQUIRE_HUMAN"
    assert judged[0][2]["source"] == "no_judge"
    assert judged[0][4] is True


def test_off_skips_everything(judged, monkeypatch):
    monkeypatch.setenv(T.MODE_ENV, "off")
    monkeypatch.setattr(CE, "_judge_backend", lambda: _backend('{"decision":"BLOCK_AND_RETRY"}'))
    assert CE._judged("shell", "rm -rf x", None) is None
    assert not judged


def _boom():
    raise RuntimeError("backend is on fire")


def test_a_judge_that_explodes_cannot_break_execution(monkeypatch):
    """It sits in front of every command, so it must never RAISE into the tool."""
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend", _boom)
    assert CE._judged("shell", "rm -rf x", None) is None, \
        "shadow is measurement: a defect in the observer must not cost a command"


def test_but_in_enforce_a_crash_in_the_judging_code_is_not_an_allow(monkeypatch):
    """THIS TEST USED TO ASSERT THE OPPOSITE, and the opposite was the defect.

    It read `assert CE._judged(...) is None` under MODE=enforce -- that is, a crash inside the
    review let the command run -- beneath a handler whose comment claimed "a crash here is not
    an allow" while returning exactly that.

    It was not hypothetical. Adding an argument to _record_judgement broke a test stub, every
    call raised TypeError, the handler swallowed it, and four tests reported an empty log
    instead of a signature error. Under enforcement that same swallow would have run an
    unjudged command because the judging code had a bug -- the precise failure this layer
    exists to prevent, one level down.
    """
    monkeypatch.setenv(T.MODE_ENV, "enforce")
    monkeypatch.setattr(CE, "_judge_backend", _boom)
    out = CE._judged("shell", "rm -rf x", None)
    assert isinstance(out, str) and "refused" in out
    assert "RuntimeError" in out, "the refusal must name the fault, or it cannot be fixed"


# ── the human, who may overrule either layer ──────────────────────────────────────────────

def _blocking_backend():
    return lambda _req: '{"decision":"BLOCK_AND_RETRY","reason":"deletes the workspace"}'


def test_an_explicit_approval_releases_a_block(judged, monkeypatch):
    """The owner's rule: 引っかかったものでも問題なしとユーザが明示的に承認したら実行OK、
    それはユーザの責任. An approval releases a BLOCK_AND_RETRY as well as a REQUIRE_HUMAN.
    """
    monkeypatch.setenv(T.MODE_ENV, "enforce")
    monkeypatch.setattr(CE, "_judge_backend", _blocking_backend)
    monkeypatch.setattr(CE, "_ask_operator", lambda text, verdict: True)
    assert CE._judged("shell", "rm -rf build", None) is None
    assert judged[0][4] is False, "an approved command must not be recorded as blocked"
    assert judged[0][5]["human"] is True


@pytest.mark.parametrize("answer", [False, None])
def test_a_decline_and_an_unreachable_human_both_keep_the_block(judged, monkeypatch, answer):
    """None means nobody could be asked -- no context, no elicitation support, a timeout. An
    unattended run that reads that as "go ahead" has inverted the point of asking."""
    monkeypatch.setenv(T.MODE_ENV, "enforce")
    monkeypatch.setattr(CE, "_judge_backend", _blocking_backend)
    monkeypatch.setattr(CE, "_ask_operator", lambda text, verdict: answer)
    out = CE._judged("shell", "rm -rf build", None)
    assert isinstance(out, str) and "refused" in out
    assert judged[0][4] is True
    assert judged[0][5]["human"] is answer


def test_shadow_never_interrupts_anyone(judged, monkeypatch):
    """A prompt is a change in behaviour, and shadow exists to measure without one. It is also
    how the layer would get switched off before it had been measured once."""
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend", _blocking_backend)
    asked = []
    monkeypatch.setattr(CE, "_ask_operator",
                        lambda text, verdict: asked.append(text) or True)
    assert CE._judged("shell", "rm -rf build", None) is None
    assert not asked, "shadow mode asked the operator a question"
    assert judged[0][5]["human"] is None, "nobody was asked; that is not a decline"


def test_the_record_tells_never_asked_apart_from_declined(judged, monkeypatch):
    """A boolean cannot hold three states, and the audit needs all three: never asked,
    approved, declined."""
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend",
                        lambda: _backend('{"decision":"ALLOW","reason":"fine"}'))
    CE._judged("shell", "rm -rf build", None)
    assert judged[0][5]["human"] is None


def test_the_deterministic_flags_are_passed_to_the_judge(judged, monkeypatch):
    """The model is told what the machine already knows, so it is not re-deriving it."""
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    seen = {}

    def _capture(req_json):
        seen.update(json.loads(req_json))
        return '{"decision":"ALLOW"}'
    monkeypatch.setattr(CE, "_judge_backend", lambda: _capture)
    CE._judged("shell", "rm -rf /var/data", None)
    assert "destructive_shell" in seen.get("deterministic_flags", [])


def test_the_request_never_carries_agent_prose(monkeypatch):
    seen = {}

    def _capture(req_json):
        seen.update(json.loads(req_json))
        return '{"decision":"ALLOW"}'
    monkeypatch.setenv(T.MODE_ENV, "shadow")
    monkeypatch.setattr(CE, "_judge_backend", lambda: _capture)
    monkeypatch.setattr(CE, "_record_judgement", lambda *a, **k: None)
    CE._judged("shell", "rm -rf x", None)
    assert set(seen) >= {"pending_command", "cwd", "deterministic_flags"}
    assert "assistant" not in json.dumps(seen)
