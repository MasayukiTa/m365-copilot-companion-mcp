# -*- coding: utf-8 -*-
"""未 `git add` のファイルは、掃くガード全部から**同時に**見えない。

## 2026-09-22 の赤

新しいテストを書き、ローカルで緑、`git add` してコミット、push。
CI の decode ラチェットが落ちた — その新しいファイルの `text=True` で。
ローカルで通ったのは欠陥ではない: ラチェットの走査は `git ls-files`（＝インデックス）に
限定されていて、**それは正しい**。CI は tracked しか見ないので、ローカル限定のファイルを
掃けば「CI が再現できない赤」を作る。

正しい判断の代償が、**add する前のファイルはどのガードからも見えない**こと。

## 既にあった半分と、足りなかった半分

2026-09-14 に同じ形で落ちたとき、`check_ci_test_manifest.py` に `--strict-untracked` が
入った。**しかしそれはマニフェストのフラグ**で、`git ls-files` を見るガードは十数個ある。
危険なのは「未追跡のテストファイル」ではなく「掃かれる root の下の未追跡ファイル」。
"""
from __future__ import annotations

import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import check_nothing_new_is_invisible as C  # noqa: E402

PREFLIGHT = io.open(os.path.join(REPO, "scripts", "preflight.py"), encoding="utf-8").read()


def _run(monkeypatch, capsys, others, argv):
    monkeypatch.setattr(C, "_git", lambda *a: list(others))
    rc = C.main(argv)
    return rc, capsys.readouterr().out


def test_a_new_source_file_under_a_swept_root_is_reported(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, ["tools/brand_new.py"], ["--strict"])
    assert rc == 1
    assert "tools/brand_new.py" in out


def test_without_strict_it_is_a_note_and_not_a_refusal(monkeypatch, capsys):
    """**書いている最中の未追跡ファイルは正常。**手で走らせたときに赤くするのは狼少年で、
    狼少年の信号は読まれなくなる — このリポジトリが preflight の `--quick` で学んだこと。"""
    rc, out = _run(monkeypatch, capsys, ["tools/brand_new.py"], [])
    assert rc == 0
    assert "NOTE only" in out


def test_a_file_outside_the_swept_roots_is_not_a_finding(monkeypatch, capsys):
    """どのガードも読まない場所の新規ファイルで止めれば、それは仕事の発明。"""
    rc, out = _run(monkeypatch, capsys, ["docs/new_note.py", "notes/scratch.py"], ["--strict"])
    assert rc == 0, out


def test_a_file_no_guard_parses_is_not_a_finding(monkeypatch, capsys):
    """.md や .json を掃くラチェットは無い。"""
    rc, out = _run(monkeypatch, capsys, ["tools/NOTES.md", "scripts/data.json"], ["--strict"])
    assert rc == 0, out


def test_every_swept_root_is_covered():
    """**root の取りこぼしがそのまま盲点になる。**掃くガード側の ROOTS と同じ集合であること。"""
    import importlib
    decode = importlib.import_module("tools.test_child_output_is_not_decoded_by_luck")
    assert set(decode.ROOTS) <= set(C.ROOTS), (
        "the decode ratchet sweeps roots this check does not watch: %r"
        % sorted(set(decode.ROOTS) - set(C.ROOTS)))


def test_preflight_runs_it_strictly():
    """**push 直前の一回に効かなければ意味がない。**手で走らせるだけの検査は、
    2026-09-14 に画面に正しい文を出しながら push を止められなかったものと同じ形。"""
    assert "check_nothing_new_is_invisible.py" in PREFLIGHT, \
        "preflight no longer runs this at all"
    i = PREFLIGHT.index("check_nothing_new_is_invisible.py")
    assert "--strict" in PREFLIGHT[i:i + 120], \
        "preflight runs it in NOTE mode, where it cannot stop a push"
