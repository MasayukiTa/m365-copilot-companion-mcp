"""The redactor has to remove paths it was never shown, on machines it has never run on.

None of these tests contains the account name it is defending against -- the check in
`scripts/check_no_identifying_names.py` scans tracked files, and a test that spelled out the
real string would be the leak it is testing for. The names below are synthetic.
"""
import os

import pytest

from bench.companionbench import redact as RD


@pytest.mark.parametrize("path", [
    r"C:\Users\someone\thing\x.py",
    r"c:\users\someone\thing\x.py",
    "/home/someone/thing/x.py",
    "/Users/someone/thing/x.py",
    r"C:/Users/someone/thing/x.py",
    r"C:\\Users\\someone\\thing\\x.py",   # as it appears once JSON has doubled the separators
])
def test_a_home_directory_goes_whatever_shape_it_arrives_in(path):
    out = RD.redact("Traceback: File %s, line 3" % path)
    assert "someone" not in out
    assert "<home>" in out
    # The part that is not identifying survives, because that is what makes a trace useful.
    assert "thing" in out and "line 3" in out


def test_the_checkout_directory_goes_too_and_goes_first():
    """The checkout lives inside the home, and its NAME is the part that carries the org.

    Redacting the home first would leave `<home>/checkout-name/...` -- still a leak, and a
    quieter one, because the obvious identifier is gone and the file looks clean.
    """
    inside = os.path.join(RD._ROOT, "bench", "companionbench", "runner.py")
    out = RD.redact("File %r, line 9" % inside)
    assert "<repo>" in out
    assert os.path.basename(RD._ROOT) not in out
    assert "runner.py" in out


def test_it_does_not_need_to_know_the_account_name():
    """Shape, not literals: a name it has never seen is removed by the same rule."""
    out = RD.redact(r"C:\Users\an-account-nobody-configured\notes.txt")
    assert "an-account-nobody-configured" not in out


def test_text_with_nothing_local_in_it_is_returned_unchanged():
    for benign in ("", "all 4 episodes passed", "bench/companionbench/runner.py:120",
                   "/usr/lib/python3.10/json/decoder.py"):
        assert RD.redact(benign) == benign


def test_deep_walks_values_lists_and_keys():
    """A workdir used as a dict KEY is the case a value-only walk misses."""
    blob = {
        "details": {"trace": r"File C:\Users\someone\a.py"},
        "rows": [{"reason": "/home/someone/b.py exploded"}],
        r"C:\Users\someone\wd": {"files": 3},
    }
    out = RD.redact_deep(blob)
    assert "someone" not in repr(out)
    assert out["rows"][0]["reason"].endswith("b.py exploded")
    assert list(out.values())[-1] == {"files": 3} or out["<home>/wd"] == {"files": 3}


def test_non_strings_survive_the_walk():
    blob = {"score": 1.0, "ok": True, "n": 3, "none": None, "t": ("a", 1)}
    assert RD.redact_deep(blob) == blob


def test_the_traceback_of_a_crashed_grader_is_redacted_where_it_is_captured():
    """The regression that caused this module: `_infra` recorded a raw traceback.

    Redacting only at the write boundary is not enough -- the same object is printed to a
    terminal and quoted into commit messages, both of which are outside that boundary.
    """
    from bench.companionbench import runner as R

    class _Ep:
        episode_id, category = "ep", "functional"

    row = R._infra(_Ep(), reason=r"boom in C:\Users\someone\x.py", started=0.0,
                   trace="Traceback (most recent call last):\n  File "
                         r'"C:\Users\someone\x.py", line 1')
    assert "someone" not in row["details"]["trace"]
    assert "someone" not in row["details"]["reason"]
    assert "Traceback" in row["details"]["trace"]
