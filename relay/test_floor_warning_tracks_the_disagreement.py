# -*- coding: utf-8 -*-
"""やること#3の固定: floor_warnings の「食い違い」警告は、食い違いが続く限り出続け、
食い違いが無くなった瞬間に消える。

測定事実(2026-10-27 09:00)の構成を再現する:
- 実行側が読むファイル(worktree の .config/settings.txt)には floor 行が無い -> 既定が適用
- パネル側のファイル(main checkout の .config/settings.txt)は disk_floor_gb=1 / ram_floor_mb=512
このとき status.json に出ていた 2 件の WARNING と同じものが floor_warning() から出ること、
そしてパネル側を実際に適用値へ一致させると WARNING が空になることを、対で確かめる。
これは relay_fleet.floor_warning() が status.json(_snapshot)に積む文字列そのもの。
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from relay import relay_fleet as RF  # noqa: E402
from tools import settings_path as SP  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_provenance(monkeypatch):
    monkeypatch.setattr(RF, "FLOOR_PROVENANCE", {})
    monkeypatch.delenv("SWE_DISK_FLOOR_GB", raising=False)


@pytest.fixture
def split(tmp_path, monkeypatch):
    """実行側が読む file(floor 行なし)とパネル側 file(floor あり)の 2 枚。測定事実の形。"""
    read = tmp_path / "wt" / ".config" / "settings.txt"
    panel = tmp_path / "main" / ".config" / "settings.txt"
    for p in (read, panel):
        p.parent.mkdir(parents=True)
    read.write_text("maxtabs=11\n", encoding="utf-8")           # floor 行なし -> 既定
    panel.write_text("disk_floor_gb=1\nram_floor_mb=512\n", encoding="utf-8")
    monkeypatch.setattr(SP, "settings_file", lambda: str(read))
    monkeypatch.setattr(SP, "old_path", lambda: str(tmp_path / "none" / "settings.txt"))
    monkeypatch.setattr(SP, "main_checkout_root", lambda repo=None: str(tmp_path / "main"))
    return read, panel


def test_disk_warning_is_present_while_files_disagree(split):
    _read, panel = split
    RF.set_floor_provenance("disk", "default")
    w = RF.floor_warning("disk", 0.0)                           # 実行側は既定(0)で動いている
    assert w.startswith("WARNING")
    assert str(panel) in w and "disk_floor_gb=1" in w


def test_ram_warning_is_present_while_files_disagree(split):
    _read, panel = split
    RF.set_floor_provenance("ram", "default")
    w = RF.floor_warning("ram", 0.0)
    assert w.startswith("WARNING")
    assert str(panel) in w and "ram_floor_mb=512" in w


def test_both_warnings_clear_the_moment_the_files_agree(split):
    """食い違いが無くなれば(実行側がパネルの値を適用すれば)、警告は消える。"""
    _read, _panel = split
    # パネルの値がそのまま適用された状態 = applied が panel と一致
    RF.set_floor_provenance("disk", "settings")
    RF.set_floor_provenance("ram", "settings")
    assert RF.floor_warning("disk", 1.0) == ""
    assert RF.floor_warning("ram", 512.0) == ""


def test_warning_persists_until_the_value_actually_matches(split):
    """中途半端に別の値へ変えても(まだ食い違うなら)警告は出続ける。消えるのは一致した時だけ。"""
    _read, panel = split
    RF.set_floor_provenance("disk", "default")
    # 2 ではまだ panel の 1 と食い違う -> 出続ける
    assert RF.floor_warning("disk", 2.0).startswith("WARNING")
    # panel 側を 2 に合わせてはじめて消える
    panel.write_text("disk_floor_gb=2\nram_floor_mb=512\n", encoding="utf-8")
    assert RF.floor_warning("disk", 2.0) == ""
