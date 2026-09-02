"""The setup transcript must not carry the credentials it just generated.

bootstrap's log() writes the console AND .setup/bootstrap.log, and two calls handed it a
freshly minted Bearer token and unlock password. That transcript exists so an operator can
send it when setup fails -- which makes it the worst file in this project to leave a
credential in. Three high-severity code-scanning alerts pointed at exactly this, and they were
right.

The console line stays: the operator has to read those values once to paste them into Copilot
Studio. Only the durable copy goes.
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.bootstrap as B


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    import pathlib
    p = pathlib.Path(str(tmp_path / "bootstrap.log"))
    monkeypatch.setattr(B, "TRANSCRIPT", p)
    return p


def _text(p):
    return io.open(str(p), encoding="utf-8").read() if p.exists() else ""


def test_a_normal_line_is_still_recorded(transcript, capsys):
    # The transcript was added because a window vanished and there was nothing to read. It has
    # to keep working for everything that is not a secret.
    B.log("    OK: created .venv")
    assert "created .venv" in _text(transcript)
    assert "created .venv" in capsys.readouterr().out


def test_a_secret_line_is_shown_but_not_recorded(transcript, capsys):
    B.log("    Your unlock password:           pw-TESTONLY-abcd1234", transcribe=False)
    out = capsys.readouterr().out
    assert "pw-TESTONLY-abcd1234" in out, "the operator could no longer read the value"
    assert "pw-TESTONLY-abcd1234" not in _text(transcript)


def test_the_credential_lines_use_it(transcript):
    # Source-level and stated as such: writing .env end to end would generate real credentials
    # and touch the operator's own file. What is checked is that neither call site can reach
    # the transcript, which is the property the alerts were about.
    import inspect
    src = inspect.getsource(B)
    for label in ("Your Bearer token (MCP_API_KEY): ", "Your unlock password:"):
        i = src.index(label)
        line_end = src.index(chr(10), i)
        line = src[src.rindex(chr(10), 0, i) + 1:line_end]
        assert "transcribe=False" in line, "this line still reaches the transcript: %s" % line.strip()


def test_the_transcript_still_says_the_credentials_were_shown(transcript):
    # "No secret in this file" and "a secret was deliberately kept out of this file" are
    # different facts, and the second one is the one worth recording.
    import inspect
    src = inspect.getsource(B)
    assert "were shown on screen" in src


def test_transcribe_defaults_to_recording(transcript):
    # The safe default is the one that keeps the diagnostic value; opting out is per call.
    B.log("plain line")
    assert "plain line" in _text(transcript)


def test_a_failing_transcript_still_never_raises(monkeypatch, capsys):
    # Unchanged property, re-checked because log() was touched: a logging call has taken a run
    # down in this project before.
    import pathlib
    monkeypatch.setattr(B, "TRANSCRIPT", pathlib.Path("Z:/nope/nowhere/bootstrap.log"))
    B.log("still fine")
    assert "still fine" in capsys.readouterr().out
