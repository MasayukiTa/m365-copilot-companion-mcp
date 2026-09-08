"""Does the guard now see identifiers that rode in on the COMMIT METADATA, not a file?

The leak that prompted this was a real name and an employer domain in a commit's author and
committer lines. A checker that only greps tracked files is blind to it, because nothing was
ever written into a file. These tests build real commits with bad and good author/committer
and call the extended checker to confirm the three existing rules -- and only those -- now
reach the metadata, while the repository's own noreply address is treated as the safe form it
is. Identifiers are ASSEMBLED FROM FRAGMENTS so no token in this file is itself an instance of
what the guard exists to catch (which would make this file fail the guard it tests).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_names_meta", os.path.join(os.path.dirname(__file__), "check_no_identifying_names.py"))
C = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(C)

#: The SAFE canonical author this repository standardises on. Split so the digits and the
#: address are not a literal token here; reassembled only inside git commands.
SAFE_NAME = "MasayukiTa"
SAFE_EMAIL = "4699" + "0346" + "+" + "MasayukiTa" + "@users.noreply.github.com"

#: An employee-id of the SHAPE under test, belonging to nobody, assembled from fragments so no
#: token in this file matches its own pattern.
SYNTHETIC_ID = "Q470" + "B2951"


def _init(d, name=SAFE_NAME, email=SAFE_EMAIL):
    subprocess.run(["git", "init", "-q", d], check=False)
    subprocess.run(["git", "-C", d, "config", "user.name", name], check=False)
    subprocess.run(["git", "-C", d, "config", "user.email", email], check=False)


def _commit(d, message, name=SAFE_NAME, email=SAFE_EMAIL, filename="f.txt", body="ok\n"):
    with open(os.path.join(d, filename), "w", encoding="utf-8") as fh:
        fh.write(body)
    subprocess.run(["git", "-C", d, "add", filename], check=False)
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    subprocess.run(["git", "-C", d, "commit", "-q", "-m", message], env=env, check=False)


def _repo():
    return tempfile.mkdtemp(prefix="idmeta_")


# ---- what it now catches in metadata -------------------------------------------------------

def test_a_configured_name_in_the_author_is_caught():
    """The exact leak: a real name in the author line, invisible to a file grep."""
    d = _repo()
    _init(d)
    _commit(d, "initial", name="acme person", email="person@acme.example")
    got = C.commit_metadata_offences(d, names=["acme"], rev_range="HEAD")
    fields = {f[1] for f in got}
    assert any("author name" in f for f in fields)
    assert any("configured name" in f for f in fields)


def test_a_configured_name_in_the_committer_email_is_caught():
    d = _repo()
    _init(d)
    _commit(d, "initial", name="Someone", email="someone@acme.example")
    got = C.commit_metadata_offences(d, names=["acme"], rev_range="HEAD")
    assert any("email" in f[1] and "configured name" in f[1] for f in got)


def test_an_employee_id_in_the_commit_message_is_caught_by_shape():
    """No configured names needed: the id shape alone fires, on the message text."""
    d = _repo()
    _init(d)
    _commit(d, "fix for %s" % SYNTHETIC_ID)
    got = C.commit_metadata_offences(d, names=[], rev_range="HEAD")
    assert any("commit message: employee-id shape" == f[1] for f in got)


def test_a_home_path_in_the_commit_message_is_caught():
    d = _repo()
    _init(d)
    home = "C:/Users" + "/somebody/x"
    _commit(d, "ran from %s" % home)
    got = C.commit_metadata_offences(d, names=[], rev_range="HEAD")
    assert any("commit message: home directory path" == f[1] for f in got)


# ---- what it must never do -----------------------------------------------------------------

def test_the_repositorys_own_noreply_author_is_not_flagged():
    """The safe canonical form must pass even when the account handle is also a configured
    name -- otherwise every honest commit fails the guard added to protect it."""
    d = _repo()
    _init(d)
    _commit(d, "clean subject")
    # Even if the handle itself were on the names list, the noreply address is stripped first.
    got = C.commit_metadata_offences(d, names=[SAFE_NAME.lower()], rev_range="HEAD")
    assert [f for f in got if "email" in f[1]] == []


def test_a_clean_commit_with_no_names_configured_is_empty():
    d = _repo()
    _init(d)
    _commit(d, "just a normal subject line")
    assert C.commit_metadata_offences(d, names=[], rev_range="HEAD") == []


def test_a_bad_log_range_is_refused_rather_than_passed():
    """A git failure must raise, not return an empty list that reads as a clean result."""
    d = _repo()
    _init(d)
    _commit(d, "one")
    with pytest.raises(C.CheckFailed):
        C.commit_metadata_offences(d, names=[], rev_range="no-such-ref-xyz")


# ---- the shared rules really are shared -----------------------------------------------------

def test_scan_text_applies_the_same_three_checks():
    """The refactor's point: one function judges a file line and a commit field alike."""
    assert C.scan_text("OWNER=%s" % SYNTHETIC_ID) == "employee-id shape"
    assert C.scan_text('P="C:/Users' + '/somebody/y"') == "home directory path"
    import re
    name_re = re.compile("acme", re.I)
    assert C.scan_text("acme was here", name_re) == "configured name"
    assert C.scan_text("nothing to see") is None


def test_a_placeholder_home_in_metadata_is_not_flagged():
    """HOME_SHAPE's placeholder exemption must hold for metadata too, since scan_text is shared."""
    d = _repo()
    _init(d)
    _commit(d, "ran from C:/Users/Public/x")
    assert C.commit_metadata_offences(d, names=[], rev_range="HEAD") == []
