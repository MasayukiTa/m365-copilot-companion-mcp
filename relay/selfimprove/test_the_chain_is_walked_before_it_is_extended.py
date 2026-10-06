# -*- coding: utf-8 -*-
"""連鎖を伸ばす前に、連鎖を歩く。

`authority_ledger` はハッシュ連鎖で、**記録を書き換えれば後続が壊れる**ようにできている。
その連結を検査する `verify()` は書かれていて、テストもあって、
**リポジトリのどこからも呼ばれていなかった**（2026-09-22 実測、88件の未到達関数の1つ）。

**誰も歩かない連鎖は飾り。** 壊れた履歴の上に追記を受け付け続け、
そのたびに「continuity がある」と主張する tail を印字していた。

## 報告であって強制ではない

台帳が壊れているからといって再署名を**拒否**すると、
**何かが起きたまさにそのときに、起きたことを記録する手段を奪う**。
しかも壊れていること自体が、オペレータが再署名しようとしている対象かもしれない。
声に出して言う、が正直な半分。

## OK が意味しないこと

`verify` 自身の docstring が書いている: **途中から書き直して連鎖を貼り直した台帳は
clean に通る**し、末尾を削った台帳も通る。捕まえられるのは
「連鎖を貼り直さなかった改ざん」— 安いほうだけ。
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from relay.selfimprove import authority_ledger as L  # noqa: E402
from relay.selfimprove import frozen as F  # noqa: E402


class _Args:
    reason = "a reason"
    authorization = "the operator said so"
    force = True


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(L, "_path", lambda path=None: str(p))
    return p


def _append_one(ledger):
    L.append(L.REBLESS, reason="first", actor_claimed="test", authorization="yes")


def test_a_healthy_chain_says_nothing(ledger, capsys):
    """**警告が常に出るなら警告ではない。**"""
    _append_one(ledger)
    F._record_rebless(_Args(), {"checksums": {}}, {"checksums": {"a.py": "h"}})
    assert "does not verify" not in capsys.readouterr().out


def test_a_tampered_record_is_reported_before_the_append(ledger, capsys):
    """**欠陥そのもの。** 書き換えた台帳の上に、黙って積み増していた。"""
    _append_one(ledger)
    rows = [json.loads(l) for l in io.open(str(ledger), encoding="utf-8") if l.strip()]
    rows[-1]["reason"] = "something else entirely"        # content changed, hash left alone
    with io.open(str(ledger), "w", encoding="utf-8", newline=chr(10)) as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + chr(10))

    F._record_rebless(_Args(), {"checksums": {}}, {"checksums": {"a.py": "h"}})
    out = capsys.readouterr().out
    assert "does not verify" in out
    assert "hash does not match" in out


def test_it_still_appends_after_warning(ledger, capsys):
    """**拒否しない。** 記録する手段を、記録すべき瞬間に奪わない。"""
    _append_one(ledger)
    before = len([l for l in io.open(str(ledger), encoding="utf-8") if l.strip()])
    rows = [json.loads(l) for l in io.open(str(ledger), encoding="utf-8") if l.strip()]
    rows[-1]["reason"] = "tampered"
    with io.open(str(ledger), "w", encoding="utf-8", newline=chr(10)) as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + chr(10))

    F._record_rebless(_Args(), {"checksums": {}}, {"checksums": {"a.py": "h"}})
    after = len([l for l in io.open(str(ledger), encoding="utf-8") if l.strip()])
    assert after == before + 1, "the warning blocked the record it was warning about"
    assert "appending anyway" in capsys.readouterr().out


def test_a_verifier_that_raises_cannot_stop_the_record(ledger, monkeypatch):
    """検査が、それが守っている記録を落とせてはならない。"""
    monkeypatch.setattr(L, "verify",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable")))
    F._record_rebless(_Args(), {"checksums": {}}, {"checksums": {"a.py": "h"}})
    assert os.path.isfile(str(ledger))


def test_verify_is_honest_about_the_tampering_it_cannot_see(ledger):
    """**OK を過大に引用させない。** 連鎖を貼り直した書き換えは clean に通る。
    これは欠陥ではなく、ハッシュ連鎖が証明できることの限界そのもの。"""
    _append_one(ledger)
    rows = [json.loads(l) for l in io.open(str(ledger), encoding="utf-8") if l.strip()]
    rows[-1]["reason"] = "rewritten and re-chained"
    rows[-1]["hash"] = L._digest(rows[-1])
    with io.open(str(ledger), "w", encoding="utf-8", newline=chr(10)) as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + chr(10))
    ok, _ = L.verify()
    assert ok is True
