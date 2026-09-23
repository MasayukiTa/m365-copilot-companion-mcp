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
    #
    # RESHAPED 2026-09-24 (D1): the values are no longer printed on one line each but framed by
    # _show_secrets_box and repeated at the end of the run. The property pinned is unchanged:
    # every place a value is printed is show_only, and the box has no other output path.
    import inspect
    box = inspect.getsource(B._show_secrets_box)
    assert "show_only(" in box
    assert "log(" not in box.replace("show_only(", "") and "_transcribe(" not in box, \
        "the secrets box can reach the transcript"
    src = inspect.getsource(B.step_gen_env)
    values = ("api_key", "unlock_code", "minted_api", "minted_unlock")
    for line in src.splitlines():
        code = line.split("#", 1)[0]
        if "log(" in code or "_transcribe(" in code:
            assert not any(v in code for v in values), \
                "a credential value reaches log/_transcribe: %s" % line.strip()
    assert src.count("_remember_and_show(") == 2, "a credential display left the box"


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


# ---- the top-up path, driven rather than read ------------------------------------------
#
# The two tests above are source-level and say so. This one runs step_gen_env against a real
# .env that is missing both secrets -- the branch that MINTS them -- and looks at what the
# transcript actually received. Alert #30 pointed at _transcribe, and the reason it stayed open
# after the show_only work is that this branch fed log() a list built from the "KEY=value" lines
# and relied on `s.split("=", 1)[0]` to take the value back off. That split is correct and it is
# a sanitizer nobody can see; the names are now collected as names and never held a value.


#: SKIPPED OFF WINDOWS, AND RUN ON THE WINDOWS JOB INSTEAD -- the rule this repository already
#: applies to scripts/test_bootstrap.py for the same reason. step_gen_env protects the secret it
#: writes with DPAPI, which does not exist on the ubuntu runner, so a failure there is a missing
#: capability rather than a broken assertion. A skip on its own would retire the coverage
#: silently; it is honest only while ci.yml's windows-install-smoke job names this file.
needs_dpapi = pytest.mark.skipif(os.name != "nt",
                                 reason="step_gen_env protects with DPAPI; Windows only")


@pytest.fixture()
def repo_with_env(tmp_path, monkeypatch, transcript):
    """A .env carrying neither secret, so the top-up branch mints both."""
    monkeypatch.setattr(B, "ROOT", tmp_path)
    env = tmp_path / ".env"
    env.write_text("SOMETHING_ELSE=1\n", encoding="utf-8")
    return env


@needs_dpapi
def test_the_minted_secrets_do_not_reach_the_transcript(repo_with_env, transcript, capsys):
    B.step_gen_env()
    written = repo_with_env.read_text(encoding="utf-8")
    api = [l.split("=", 1)[1] for l in written.splitlines() if l.startswith("MCP_API_KEY=")]
    assert api and len(api[0]) == 40, written          # it really did mint one
    recorded = io.open(str(transcript), encoding="utf-8").read()
    assert api[0] not in recorded, "the freshly minted Bearer token is in the transcript"


@needs_dpapi
def test_the_transcript_still_names_the_keys_it_generated(repo_with_env, transcript):
    """Withholding the values must not cost the operator the record of WHICH secrets setup
    created -- that is the line they read when a key they expected is missing."""
    B.step_gen_env()
    recorded = io.open(str(transcript), encoding="utf-8").read()
    assert "MCP_API_KEY" in recorded
    assert B.UNLOCK_PASSWORD_PROTECTED_VAR in recorded


@needs_dpapi
def test_the_unlock_password_is_stored_only_in_its_protected_form(repo_with_env, capsys):
    """Not about the transcript: the value printed on screen must not also be sitting in .env.
    If these ever became equal, the show_only work would be protecting a file that already has
    the cleartext in it."""
    B.step_gen_env()
    shown = capsys.readouterr().out
    written = repo_with_env.read_text(encoding="utf-8")
    line = [l for l in written.splitlines()
            if l.startswith(B.UNLOCK_PASSWORD_PROTECTED_VAR + "=")]
    assert line, written
    stored = line[0].split("=", 1)[1]
    assert stored not in shown or stored == "", "the stored form equals what was displayed"
