"""The redactor has to remove paths it was never shown, on machines it has never run on.

The account name in these fixtures is "example", which `scripts/check_no_identifying_names.py`
treats as identifying nobody. That constraint is not an annoyance to work around: this file is
tracked in a public repository, and a test that spelled out a real account name would BE the
leak it is testing for -- which is how the last two attempts at a fixture for this went. Where
a test genuinely needs a name the redactor has never seen, it is assembled from fragments so
that no line of this file ever contains a home-directory shape with a real-looking name in it.
"""
import os

import pytest

from bench.companionbench import redact as RD


@pytest.mark.parametrize("path", [
    r"C:\Users\example\thing\x.py",
    r"c:\users\example\thing\x.py",
    "/home/example/thing/x.py",
    "/Users/example/thing/x.py",
    "C:/Users/example/thing/x.py",
    r"C:\\Users\\example\\thing\\x.py",   # as it appears once JSON has doubled the separators
])
def test_a_home_directory_goes_whatever_shape_it_arrives_in(path):
    out = RD.redact("Traceback: File %s, line 3" % path)
    assert "example" not in out
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
    """Shape, not literals: a name it has never seen is removed by the same rule.

    Assembled at run time. Written as one literal, this line would carry a home-directory
    shape with an unrecognised name in it, and the name check would flag this file -- rightly,
    since it cannot tell a fixture from the real thing.
    """
    unseen = "C:\\Users\\" + "an-account" + "-nobody-configured" + "\\notes.txt"
    out = RD.redact(unseen)
    assert "an-account" not in out
    assert out == "<home>\\notes.txt"


def test_text_with_nothing_local_in_it_is_returned_unchanged():
    for benign in ("", "all 4 episodes passed", "bench/companionbench/runner.py:120",
                   "/usr/lib/python3.10/json/decoder.py"):
        assert RD.redact(benign) == benign


def test_deep_walks_values_lists_and_keys():
    """A workdir used as a dict KEY is the case a value-only walk misses."""
    blob = {
        "details": {"trace": r"File C:\Users\example\a.py"},
        "rows": [{"reason": "/home/example/b.py exploded"}],
        r"C:\Users\example\wd": {"files": 3},
    }
    out = RD.redact_deep(blob)
    assert "example" not in repr(out)
    assert out["rows"][0]["reason"].endswith("b.py exploded")
    assert out["<home>\\wd"] == {"files": 3}


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

    row = R._infra(_Ep(), reason=r"boom in C:\Users\example\x.py", started=0.0,
                   trace="Traceback (most recent call last):\n  File "
                         r'"C:\Users\example\x.py", line 1')
    assert "example" not in row["details"]["trace"]
    assert "example" not in row["details"]["reason"]
    assert "Traceback" in row["details"]["trace"]


# ---------------------------------------------------------------------------
# Every case below was found by adversarial review, reproduced against the first version of
# this module, and then fixed. They are the tests that matter: the ones above were written by
# the same person who wrote the code and all passed while the module leaked four ways.
# ---------------------------------------------------------------------------

def test_an_account_name_with_a_space_does_not_leak_the_rest_of_the_path():
    """The first version cut at the space, leaving the surname and the whole path after it."""
    # Assembled: a two-word account name written as one literal would put a home shape with
    # an unrecognised name into a tracked file, and the name check would flag it -- rightly.
    spaced = "C:\\Users\\" + "Given" + " " + "Family" + "\\secret\\x.py"
    out = RD.redact(spaced)
    assert out == r"<home>\secret\x.py"
    assert "Family" not in out


def test_the_account_segment_stops_at_a_quote_so_a_traceback_line_survives():
    quoted = 'File "' + "C:\\Users\\" + "Given" + " " + "Family" + "\\x.py" + '", line 3'
    assert RD.redact(quoted) == 'File "' + r"<home>\x.py" + '", line 3'


def test_a_placeholder_is_not_eaten_a_second_time():
    assert RD.redact(r"<home>\wd") == r"<home>\wd"


def test_a_checkout_outside_any_home_is_still_removed_whatever_the_case(monkeypatch):
    """On Windows paths are case-insensitive; the first version's replacement was not.

    A checkout in a build directory matched neither the exact string nor the home shape, so
    the whole path survived -- the case that CI itself would hit.
    """
    monkeypatch.setattr(RD, "_ROOT", r"D:\build\OrgRepo")
    assert RD.redact(r"d:\BUILD\orgrepo\bench\x.py") == r"<repo>\bench\x.py"


def test_a_path_object_is_redacted_rather_than_left_for_default_str():
    """`json.dump(default=str)` runs AFTER the walk. A Path that survives it is written raw.

    This is the original bug one type away, which is why it is tested against the writer and
    not against the redactor alone.
    """
    import json
    import pathlib
    blob = {"trace": pathlib.Path(r"C:\Users\example\secret.py"),
            "raw": rb"C:\Users\example\secret.py"}
    written = json.dumps(RD.redact_deep(blob), default=str)
    assert "example" not in written
    assert written.count("<home>") == 2


def test_two_different_homes_do_not_collapse_into_one_row():
    """Redacting keys made two records one, and one silently won. Evidence, destroyed."""
    one = "C:\\Users\\" + "one-account" + "\\x"
    other = "C:\\Users\\" + "other-account" + "\\x"
    out = RD.redact_deep({one: 1, other: 2})
    assert out["_redaction_collisions"] == 2
    values = [v for k, v in out.items() if k != "_redaction_collisions"]
    assert sorted(values) == [1, 2], "both rows survive"
    assert "one-account" not in repr(out) and "other-account" not in repr(out)
