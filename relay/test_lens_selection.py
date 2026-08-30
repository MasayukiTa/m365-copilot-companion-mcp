"""Which lens a run gets, and the two ways it was silently the wrong one.

Both were found by reading the first output of the mechanism telemetry rather than by reading
the code -- the instrument's first job was to make "the panel ran on 4.3% of records" mean
something, and what it meant was not "nobody wanted it".
"""
import inspect
import re

from relay import fleet_runner as FR
from relay.refuter import PANEL_LENSES


def _src():
    """The source with comments and docstrings stripped.

    ASSERTING ON RAW SOURCE MATCHES THE COMMENT THAT EXPLAINS THE REMOVAL. This file's first
    version checked that `if args.panel and args._lenses is None` was absent, and it failed --
    because the comment recording that it had been removed contains the string. That is a
    rule this repository already carries, broken again here.
    """
    src = inspect.getsource(FR)
    out = []
    for line in src.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # A trailing comment can carry the same text; cut at an unquoted '#'.
        q = None
        cut = None
        for i, ch in enumerate(line):
            if q:
                if ch == q:
                    q = None
            elif ch in "'\"":
                q = ch
            elif ch == "#":
                cut = i
                break
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def test_explicit_panel_is_not_conditional_on_the_lens_being_unset():
    """THE FIRST DEFECT.

    `if args.panel and args._lenses is None` meant --panel was ignored whenever the effort
    level had already chosen a lens -- which `auto`, the default, always does. Asking for the
    panel on a default run produced a single reviewer and said nothing about it."""
    src = _src()
    assert "if args.panel and args._lenses is None" not in src
    assert re.search(r"^\s*if args\.panel:\s*$", src, re.M), "the flag must stand alone"


def test_an_override_says_what_it_overrode():
    """A silent override is the same failure as a silent ignore, one step later."""
    src = _src()
    assert "--panel overrides the effort's lens" in src


def test_a_coding_run_gets_the_coding_lens_even_without_the_domain_flag():
    """THE SECOND DEFECT.

    `auto` picks rootcause_code only when SWE_MINIMALITY or MCP_TASK_DOMAIN says so. The CLI
    SWE path sets it; the UI-driven path never has, so 155 of 155 ledger records carry the
    domain-agnostic `rootcause`. The refuter ran and asked the wrong questions."""
    src = _src()
    assert 'args._lenses == ["rootcause"]' in src
    assert '"rootcause_code"' in src
    assert "fixing a real bug in the open-source project" in src


def test_a_mixed_run_is_not_reviewed_with_code_criteria():
    """fleet_runner's own rule, in the other direction: a non-coding task must never get code
    criteria. So the correction requires EVERY goal to be a coding goal, not merely one."""
    src = _src()
    assert "_coding_goals == len(_texts)" in src
    assert "a mixed run must not be reviewed with code criteria" in src


def test_the_correction_is_announced():
    """The original mistake survived because nothing said which lens was in use."""
    src = _src()
    assert "lens corrected" in src


def test_the_panel_constant_is_still_the_three_lenses():
    """If this changes, both tests above are asserting about something else."""
    assert list(PANEL_LENSES) == ["correctness", "edge", "security"]
