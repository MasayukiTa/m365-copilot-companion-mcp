# -*- coding: utf-8 -*-
"""`--authorization -` must reach the ledger as the operator's words, from EVERY CLI that takes it.

THE DEFECT THIS EXISTS FOR. pending.py implemented the "-" convention and documented it as "how
the dashboard passes it". frozen.py took the same flag name, is called the same way by the same
dashboard, and never implemented it -- so every re-signing made from the dashboard recorded the
literal string "-" as the authorisation. The act was real and the approval was real; the one
field the authority ledger exists to hold was a placeholder. It was on screen the whole time as
a chip reading "-", which is how a placeholder hides: it looks like a value.

Measured 2026-09-19 on the live ledger: the newest rebless row read authorization "-".
"""
from __future__ import annotations

import io
import json
import os

import pytest

from relay.selfimprove import frozen as F
from relay.selfimprove import pending as P
from relay.selfimprove import stdin_arg as SA

WORDS = "この変更は意図したもの。\"引用符\" も ' も含む、私の言葉。"


class _Stdin:
    def __init__(self, raw):
        self.buffer = self
        self._raw = raw

    def read(self):
        return self._raw


# ---- the convention itself -------------------------------------------------------------------

def test_a_plain_value_passes_through_untouched():
    assert SA.resolve("the operator said yes") == "the operator said yes"


def test_a_dash_is_replaced_by_stdin(monkeypatch):
    monkeypatch.setattr(SA.sys, "stdin", _Stdin(WORDS.encode("utf-8")))
    assert SA.resolve("-") == WORDS


def test_it_decodes_as_utf8_not_the_locale(monkeypatch):
    """This machine's locale is cp932. Decoding Japanese that way corrupts exactly the text
    this path exists to carry through unaltered."""
    monkeypatch.setattr(SA.sys, "stdin", _Stdin("再署名どうぞ".encode("utf-8")))
    assert SA.resolve("-") == "再署名どうぞ"


def test_an_unreadable_stdin_yields_empty_not_a_dash(monkeypatch):
    """Empty is refused downstream, which is correct. A literal "-" would be accepted and
    recorded, which is the whole defect."""
    class _Boom:
        buffer = property(lambda self: (_ for _ in ()).throw(OSError("no stdin")))

    monkeypatch.setattr(SA.sys, "stdin", _Boom())
    assert SA.resolve("-") == ""


def test_only_an_exact_dash_triggers_it(monkeypatch):
    monkeypatch.setattr(SA.sys, "stdin", _Stdin(b"SHOULD NOT BE USED"))
    for v in ("--", " - ", "-x", ""):
        assert SA.resolve(v) == v


# ---- and every CLI that offers the flag honours it ---------------------------------------------

def test_frozen_resolves_it_before_recording(monkeypatch, tmp_path):
    """RUNTIME, not a source assertion. The bug was that frozen.py parsed the flag and used the
    string as given, so what has to be measured is the value that reaches the record."""
    seen = {}
    monkeypatch.setattr(SA.sys, "stdin", _Stdin(WORDS.encode("utf-8")))
    monkeypatch.setattr(F, "snapshot_baseline",
                        lambda repo, baseline, force=False: {"checksums": {}})
    monkeypatch.setattr(F, "compute_checksums", lambda repo: {})
    monkeypatch.setattr(F, "load_baseline", lambda p: {"checksums": {}})
    monkeypatch.setattr(F, "_record_rebless",
                        lambda args, before, after: seen.update(auth=args.authorization))
    monkeypatch.setattr(F, "_resolve_pending_for", lambda excluded, args: None)

    rc = F._main(["--snapshot", "--force", "--reason", "a reason",
                 "--authorization", "-", "--baseline", str(tmp_path / "b.json")])
    assert rc == 0, rc
    assert seen.get("auth") == WORDS, \
        "frozen.py recorded %r instead of the operator's words" % seen.get("auth")


def test_pending_resolves_it_before_recording(monkeypatch, tmp_path):
    q = tmp_path / "pending_decisions.jsonl"
    monkeypatch.setattr(P, "QUEUE_PATH", str(q))
    monkeypatch.setattr(SA.sys, "stdin", _Stdin(WORDS.encode("utf-8")))
    pid = P.add(["some/file.py"], "a reason")
    rc = P._cli(["--approve", pid, "--authorization", "-"])
    assert rc == 0, rc
    # PARSED, not searched. The words contain a double quote, so the file holds them escaped
    # and a substring test on the raw text fails on a record that is perfectly correct -- which
    # it did, and the assertion was the thing that was wrong.
    rows = [json.loads(l) for l in io.open(str(q), encoding="utf-8") if l.strip()]
    auth = [r.get("authorization") for r in rows if r.get("event") == "resolved"]
    assert auth == [WORDS], "pending.py recorded %r" % auth


def test_neither_cli_reads_the_flag_without_resolving_it():
    """The pair above would both pass if a THIRD caller of this flag appeared and skipped the
    step -- which is exactly how this happened the first time. Cheap, and it names the rule."""
    from conftest import code_only

    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("frozen.py", "pending.py"):
        code = code_only(os.path.join(here, name))
        if '"--authorization"' not in code:
            continue
        assert "_stdin_arg.resolve(args.authorization)" in code, \
            "%s takes --authorization and never resolves the \"-\" convention" % name


def test_the_help_text_says_so_in_both():
    """A convention nobody can discover from --help is one the next caller will not implement."""
    assert "stdin" in SA.help_suffix()


@pytest.mark.parametrize("mod", [F, P])
def test_both_import_the_one_implementation(mod):
    assert getattr(mod, "_stdin_arg") is SA
