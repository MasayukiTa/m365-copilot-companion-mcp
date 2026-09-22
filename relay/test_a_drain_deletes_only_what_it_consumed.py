# -*- coding: utf-8 -*-
"""自分が扱えないコマンドを、読んで消すな。

`local_loop_controller._drain_commands` と
`bench.local_review_transport._consume_console_commands` は、どちらも
**ファイルを読み、無条件に unlink し、そのあと `stop` / `close` だけを見ていた。**
同じファイルに入っていた `add_goal` も `steer` も `set_maxtabs` も、記録を残さず消える。
**消えた goal は、そもそも送られなかった goal と見分けがつかない。**

どちらも普段は自分の state_dir を読む。**だがそれを強制するものは無い** —
`--state-dir` / `state_dir` は引数で、`.fleet` に向ければフリート本体のコマンドチャネルを
読み手と競争しながら削除して回る。そして**削除は必ず勝つ。**

規則を「**完全に消費したものだけ削除する**」に変えた。扱えないキーがあれば、それは
自分のチャネルではないので、**何もせず、何も消さず、一度だけ言う**。
`stop` だけ拾って残りを拒むのは、他所宛ての指示に答えることになる。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


# ── bench.local_review_transport ──────────────────────────────────────────────

def _consume(tmp_path, payload, entries=None):
    from bench import local_review_transport as LRT

    p = tmp_path / "commands.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    cancelled = []

    class _Store:
        def cancel_job(self, job_id, reason):
            cancelled.append(job_id)

    got = LRT._consume_console_commands(p, _Store(), entries or [])
    return p, got, cancelled


def test_a_goal_in_the_file_is_not_destroyed(tmp_path):
    """**欠陥そのもの。** 扱えない `add_goal` ごとファイルが消えていた。"""
    p, got, cancelled = _consume(tmp_path, {"add_goal": [{"text": "tidy the docs"}]})
    assert p.is_file(), "the console consumer deleted a command it cannot handle"
    assert json.loads(p.read_text(encoding="utf-8"))["add_goal"][0]["text"] == "tidy the docs"
    assert got == set() and cancelled == []


def test_a_stop_sharing_the_file_with_a_goal_is_not_obeyed_either(tmp_path):
    """半分だけ実行して半分捨てるのは、他所宛ての指示に答えること。"""
    p, got, cancelled = _consume(
        tmp_path, {"stop": True, "add_goal": [{"text": "x"}]},
        entries=[{"worker": "w1", "job_id": "j1"}])
    assert p.is_file()
    assert cancelled == [], "it acted on a stop in a file that is not its channel"


def test_its_own_stop_still_works_and_the_file_goes(tmp_path):
    """扱えるキーだけなら、従来どおり消費して消す。"""
    p, got, cancelled = _consume(tmp_path, {"stop": True},
                                 entries=[{"worker": "w1", "job_id": "j1"}])
    assert not p.is_file()
    assert got == {"w1"} and cancelled == ["j1"]


def test_an_unparseable_file_is_kept(tmp_path):
    """**何が入っていたか誰も知らない**のが、まさに消してはいけない理由。"""
    from bench import local_review_transport as LRT

    p = tmp_path / "commands.json"
    p.write_text("{not json", encoding="utf-8")
    LRT._consume_console_commands(p, object(), [])
    assert p.is_file()


# ── relay.local_loop_controller ───────────────────────────────────────────────

class _Ctl:
    """`_drain_commands` だけを本物から借りる。実体は重い依存を抱えているので、
    この検査に要る状態だけ持たせる。"""

    def __init__(self, path, job_id="j1"):
        from relay import local_loop_controller as LLC

        self.commands_path = path
        self.job_id = job_id
        self.cancelled = []
        self._HANDLED_COMMAND_KEYS = LLC.LocalLoopController._HANDLED_COMMAND_KEYS
        self._drain = LLC.LocalLoopController._drain_commands
        self.store = self
        self.driver = None

    def cancel_job(self, job_id, reason):
        self.cancelled.append(job_id)

    def _project(self):
        pass

    def drain(self):
        return self._drain(self)


def test_the_controller_keeps_a_goal_it_cannot_handle(tmp_path, capsys):
    import pathlib

    p = pathlib.Path(str(tmp_path / "commands.json"))
    p.write_text(json.dumps({"steer": [{"worker": "w", "text": "hi"}]}), encoding="utf-8")
    ctl = _Ctl(p)
    assert ctl.drain() is False
    assert p.is_file(), "the controller deleted a steer meant for the fleet"
    assert ctl.cancelled == []
    assert "leaving" in capsys.readouterr().out


def test_the_controller_still_stops_on_its_own_command(tmp_path):
    import pathlib

    p = pathlib.Path(str(tmp_path / "commands.json"))
    p.write_text(json.dumps({"stop": True}), encoding="utf-8")
    ctl = _Ctl(p)
    assert ctl.drain() is True
    assert not p.is_file()
    assert ctl.cancelled == ["j1"]


def test_a_broken_file_no_longer_takes_the_evidence_with_it(tmp_path, capsys):
    """**証拠が先に消えて報告が後に来ていた。** 古い実装は `finally` で unlink してから
    例外を外へ出していたので、パースできなかったファイルの中身は失われ、呼び出し元は
    traceback だけを受け取った。"""
    import pathlib

    p = pathlib.Path(str(tmp_path / "commands.json"))
    p.write_text("{not json", encoding="utf-8")
    ctl = _Ctl(p)
    assert ctl.drain() is False        # 例外を投げないこと自体が検査
    assert p.is_file()
    assert "will not parse" in capsys.readouterr().out
