"""The setup transcript must not carry the credentials it just generated.

bootstrap's log() writes the console AND .setup/bootstrap.log, and two calls handed it a
freshly minted Bearer token and unlock password. That transcript exists so an operator can
send it when setup fails -- which makes it the worst file in this project to leave a credential
in. Three high-severity code-scanning alerts pointed at exactly this.

WHY A SEPARATE FUNCTION AND NOT A FLAG. The first fix gave log() `transcribe=False`. The
behaviour was right and the shape was wrong: the flow from the credential into log() and on to
the file write still existed in the source, so the clear-text-storage finding stayed open, and
a later edit flipping a default would have silently restored the leak. show_only() has no path
to _transcribe at all, which is a property of the structure rather than of an argument.

The console print stays. A fresh .env stores only the PROTECTED form of the unlock password,
so that line is the one occasion the operator can read the real value; removing it would not
harden anything, it would make setup impossible to finish.
"""
import io
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.bootstrap as B


@pytest.fixture
def transcript(tmp_path, monkeypatch):
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


def test_show_only_prints_and_does_not_record(transcript, capsys):
    B.show_only("    Your unlock password:           pw-TESTONLY-abcd1234")
    out = capsys.readouterr().out
    assert "pw-TESTONLY-abcd1234" in out, "the operator could no longer read the value"
    assert "pw-TESTONLY-abcd1234" not in _text(transcript)


def test_show_only_cannot_reach_the_transcript_at_all(monkeypatch, capsys):
    # Structural, not behavioural: if _transcribe were ever called from show_only this fails,
    # whatever any flag or default happens to be set to.
    called = []
    monkeypatch.setattr(B, "_transcribe", lambda m: called.append(m))
    B.show_only("secret-ish line")
    assert called == [], "show_only reached the transcript: %s" % called
    assert "secret-ish line" in capsys.readouterr().out


def test_log_still_reaches_the_transcript(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(B, "_transcribe", lambda m: called.append(m))
    B.log("ordinary line")
    assert called == ["ordinary line"]


def test_the_credential_lines_use_show_only(transcript):
    # Source-level and stated as such: writing .env end to end would mint real credentials and
    # touch the operator's own file. What is pinned is that neither call site can reach the
    # transcript, which is what the alerts were about.
    import inspect
    src = inspect.getsource(B)
    for label in ("Your Bearer token (MCP_API_KEY): ", "Your unlock password:"):
        i = src.index(label)
        line = src[src.rindex(chr(10), 0, i) + 1:src.index(chr(10), i)]
        assert "show_only(" in line, "this line still reaches the transcript: %s" % line.strip()


def test_the_transcript_still_says_the_credentials_were_shown(transcript):
    # "No secret in this file" and "a secret was deliberately kept out of this file" are
    # different facts, and the second is the one worth recording.
    import inspect
    assert "were shown on screen" in inspect.getsource(B)


def test_a_failing_transcript_still_never_raises(monkeypatch, capsys):
    # Unchanged property, re-checked because log() was restructured: a logging call has taken a
    # run down in this project before.
    monkeypatch.setattr(B, "TRANSCRIPT", pathlib.Path("Z:/nope/nowhere/bootstrap.log"))
    B.log("still fine")
    assert "still fine" in capsys.readouterr().out
