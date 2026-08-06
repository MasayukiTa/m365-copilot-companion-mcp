"""承認を押したら反映されること、切れたら黙って消えないことを固定する。

押しても何も起きず確認画面が残り続ける、という不具合が出た。原因は2つ。

1. 押した内容を信頼状態へ取り込む処理が、Skill を一覧したときにしか走らなかった。
   誰も一覧しなければ永久に反映されない。
2. 有効期限が10分しかなく、切れると確認画面ごと黙って消えた。押した本人からは
   「承認したのに何も起きない」あるいは「消えたから通ったのだろう」としか見えない。
"""
import json
import time

import pytest

from relay.skills import APPROVAL_TTL_SECONDS, SkillStore

SKILL = """---
name: probe-skill
description: "テスト用。承認の流れだけを見る。"
---

# 手順

何もしない。
"""


def _make(tmp_path, body=SKILL):
    root = tmp_path / "proj"
    d = root / "skills" / "probe-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return root


def _store(tmp_path, root):
    # 生成後に db_path を差し替えると、表がまだ無いまま使うことになる。
    # 置き場所は生成時に渡す。
    return SkillStore(root,
                      db_path=tmp_path / "skills.sqlite3",
                      gate_dir=tmp_path / "gates")


def _click_approve(path, answer="approved"):
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update({"answered": True, "answer": answer, "answered_at": time.time()})
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_ttl_is_long_enough_for_a_person():
    # 10分だと、席を外して戻ってきた頃には切れている。実際に6分遅れで押した
    # 承認が捨てられた。
    assert APPROVAL_TTL_SECONDS >= 60 * 60


def test_sync_is_callable_from_outside():
    # discover() の中でしか動かないと、誰も一覧しない限り反映されない。
    assert hasattr(SkillStore, "sync_approvals")


def test_approval_click_becomes_trust(tmp_path):
    root = _make(tmp_path)
    store = _store(tmp_path, root)
    assert store.get("probe-skill").trust == "untrusted"

    req = store.request_approval("probe-skill")
    gate = store.gate_dir / (req["token"] + ".json")
    assert gate.is_file()
    _click_approve(gate)

    # 一覧を通さずに取り込めること（常駐が呼ぶのはこの入口）
    store.sync_approvals()
    assert store.get("probe-skill").trust == "trusted"


def test_denied_click_does_not_grant_trust(tmp_path):
    root = _make(tmp_path)
    store = _store(tmp_path, root)
    req = store.request_approval("probe-skill")
    _click_approve(store.gate_dir / (req["token"] + ".json"), answer="denied")
    store.sync_approvals()
    assert store.get("probe-skill").trust == "untrusted"


def test_expired_gate_says_it_did_not_take(tmp_path, monkeypatch):
    """期限切れを黙って消さないこと。

    消していた頃は、画面から消えた＝承認が通ったのだと読めてしまった。
    """
    root = _make(tmp_path)
    store = _store(tmp_path, root)
    req = store.request_approval("probe-skill")
    gate = store.gate_dir / (req["token"] + ".json")
    _click_approve(gate)

    # 期限を過去にする
    import sqlite3
    cn = sqlite3.connect(str(store.db_path))
    cn.execute("UPDATE approval_challenges SET expires_at=?", (time.time() - 1,))
    cn.commit()
    cn.close()

    store.sync_approvals()
    assert gate.is_file(), "確認画面ごと消してはいけない"
    payload = json.loads(gate.read_text(encoding="utf-8"))
    assert payload["outcome"] == "expired"
    assert "反映されていません" in payload["note"]
    assert store.get("probe-skill").trust == "untrusted"


def test_changed_bundle_loses_trust(tmp_path):
    """承認はそのときの中身にしか効かないこと。"""
    root = _make(tmp_path)
    store = _store(tmp_path, root)
    req = store.request_approval("probe-skill")
    _click_approve(store.gate_dir / (req["token"] + ".json"))
    store.sync_approvals()
    assert store.get("probe-skill").trust == "trusted"

    (root / "skills" / "probe-skill" / "SKILL.md").write_text(
        SKILL + "\n余計な行\n", encoding="utf-8")
    assert store.get("probe-skill").trust != "trusted"


def test_match_says_when_a_skill_is_waiting_for_reapproval(tmp_path, monkeypatch):
    """一致なしとだけ返さないこと。

    束を1文字直すと信頼が外れ、照合は信頼済みしか見ないので「一致なし」になる。
    それだけ返していたとき、呼び出し側は「そんな手順は無い」と読んで自分でやり方を
    考え始めた。手順はあって、承認待ちだっただけ。
    """
    import json as _json

    from tools import skill_ops

    root = _make(tmp_path)
    store = _store(tmp_path, root)
    req = store.request_approval("probe-skill")
    _click_approve(store.gate_dir / (req["token"] + ".json"))
    store.sync_approvals()
    assert store.get("probe-skill").trust == "trusted"

    # 承認したあとに中身を変える＝実際に起きたこと
    (root / "skills" / "probe-skill" / "SKILL.md").write_text(
        SKILL + "\n一行足した\n", encoding="utf-8")
    assert store.get("probe-skill").trust == "changed"

    monkeypatch.setattr(skill_ops, "_store", lambda: store)
    out = skill_ops.skill_match("承認の流れだけを見るテスト用")
    assert "probe-skill" in out, out
    assert "re-approval" in out, out
    # 自分でやり方を作るな、と言うところまで含めて契約
    assert "do not invent" in out
