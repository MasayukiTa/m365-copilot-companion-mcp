"""Theme-keyed memory of what was actually done, and the wiring that fills it.

The store existed and worked. What it did not have was inputs: only relay/code_task.py
ever called record_task, so 2636 fleet transcripts and 451 LOCAL_LOOP jobs had produced a
grand total of ONE memory entry, written 2026-06-12. A memory nothing writes to is
indistinguishable from a memory that is broken, so these tests pin the wiring as hard as
they pin the store.

Shape follows where Claude Code and Codex both landed: an always-read INDEX plus one
markdown file per theme, read on demand. The two-layer split is the part worth protecting
-- drop the index and every theme becomes an island that cannot lead the agent to a
neighbouring one.
"""
import re
import tempfile
from pathlib import Path

from relay.project_memory import (
    list_themes,
    load_notes,
    record_task,
    slugify,
    theme_from_goal,
)

REPO = Path(__file__).resolve().parent.parent
FLEET_SRC = (REPO / "relay" / "relay_fleet.py").read_text(encoding="utf-8")
LOOP_SRC = (REPO / "relay" / "local_loop_controller.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- the store

def test_a_theme_gets_its_own_file_and_an_index_entry():
    sd = tempfile.mkdtemp(prefix="tm_")
    assert record_task("OGF 異物解析", "分類を自動化する", "DONE", note="69件を4分類", state_dir=sd)
    files = {p.name for p in (Path(sd) / "memory").iterdir()}
    assert "INDEX.md" in files
    assert len(files) == 2, files
    index = (Path(sd) / "memory" / "INDEX.md").read_text(encoding="utf-8")
    assert "OGF 異物解析" in index


def test_two_themes_do_not_share_a_file():
    sd = tempfile.mkdtemp(prefix="tm_")
    record_task("テーマA", "作業1", "DONE", state_dir=sd)
    record_task("テーマB", "作業2", "DONE", state_dir=sd)
    assert len(list_themes(sd)) == 2
    a = load_notes("テーマA", state_dir=sd)
    assert "作業1" in a and "作業2" not in a.split("記憶している他のテーマ")[0]


def test_the_index_is_what_makes_a_neighbouring_theme_discoverable():
    """索引が無ければテーマは孤島になる。これが二層構造の要点。"""
    sd = tempfile.mkdtemp(prefix="tm_")
    record_task("隣のテーマ", "隣の作業", "DONE", state_dir=sd)
    notes = load_notes("いま見ているテーマ", state_dir=sd)
    assert "隣のテーマ" in notes
    assert "このテーマ" not in notes            # 自分の実績はまだ無い


def test_japanese_themes_do_not_all_collapse_into_one_file():
    """ASCII 前提の slug では日本語テーマが全部空文字になり、1ファイルに潰れる。"""
    a, b = slugify("異物解析"), slugify("帳票OCR")
    assert a and b and a != b


def test_newest_entry_comes_first_and_old_ones_are_capped():
    sd = tempfile.mkdtemp(prefix="tm_")
    for i in range(25):
        record_task("同じテーマ", "作業%d" % i, "DONE", state_dir=sd, ts=100 + i)
    body = load_notes("同じテーマ", max_items=99, state_dir=sd)
    assert body.index("作業24") < body.index("作業23")
    assert "作業0" not in body


def test_a_path_key_stays_folder_grouped():
    """旧来のフォルダ指定の呼び出しは、目標ごとに散らさずフォルダ単位でまとめる。"""
    sd = tempfile.mkdtemp(prefix="tm_")
    folder = tempfile.mkdtemp(prefix="proj_")
    record_task(folder, "作業1", "DONE", state_dir=sd)
    record_task(folder, "作業2", "DONE", state_dir=sd)
    assert len(list_themes(sd)) == 1
    notes = load_notes(folder, state_dir=sd)
    assert "作業1" in notes and "作業2" in notes


def test_two_folders_sharing_a_basename_stay_separate():
    sd = tempfile.mkdtemp(prefix="tm_")
    a = Path(tempfile.mkdtemp(prefix="x_")) / "test"
    b = Path(tempfile.mkdtemp(prefix="y_")) / "test"
    a.mkdir(); b.mkdir()
    record_task(str(a), "Aの作業", "DONE", state_dir=sd)
    record_task(str(b), "Bの作業", "DONE", state_dir=sd)
    assert len(list_themes(sd)) == 2


def test_a_broken_store_never_takes_down_the_caller():
    """記録の失敗が走行を壊してはならない。"""
    assert record_task("t", "g", "DONE", state_dir="\0invalid") is False
    assert load_notes("t", state_dir="\0invalid") == ""


def test_theme_from_goal_takes_the_first_clause():
    assert theme_from_goal("帳票をOCRする。そのあとExcel化する。") == "帳票をOCRする"
    assert theme_from_goal("") == "general"


def test_an_entry_records_outcome_goal_and_note():
    sd = tempfile.mkdtemp(prefix="tm_")
    record_task("t", "目標テキスト", "STUCK", note="理由の抜粋", state_dir=sd)
    line = load_notes("t", state_dir=sd)
    assert "[STUCK]" in line and "目標テキスト" in line and "理由の抜粋" in line


# ---------------------------------------------------------------- the wiring

def test_the_fleet_records_every_worker_when_a_run_ends():
    assert "from relay.project_memory import record_task" in FLEET_SRC
    tail = FLEET_SRC[FLEET_SRC.index("# make sure no tab is left behind"):]
    assert "record_task(" in tail, "fleet の完了地点で記録していない"
    assert "for w in workers:" in tail


def test_the_fleet_also_reads_memory_back():
    """記録するだけで参照しないなら意味がない。"""
    fn = FLEET_SRC[FLEET_SRC.index("def _with_theme_memory"):]
    assert "load_notes(" in fn
    # 呼び出しの「文面」ではなく「使われていること」を見る。以前は
    # "_with_theme_memory(self.goal)" という一文をそのまま探しており、手順注入を
    # 挟んで合成した時点で、挙動は正しいのに落ちた。
    assert "_with_theme_memory(" in FLEET_SRC
    assert "theme_text=self.goal" in FLEET_SRC,         "テーマは goal から導出し続けること（包んだ本文からではなく）"


def test_priming_touches_the_sent_body_not_the_worker_identity():
    """ワーカーは goal 文字列で同定される。書き換えると転写キー・再実行エンベロープ・
    record_task のテーマが同時に壊れ、実際にフリート走行がハングした。"""
    # 差し込みは初回ジョブ本文の組み立て地点でのみ行う。文字数窓で近傍を判定していたが、
    # 間にコメントが数行入っただけで窓から外れた。窓ではなく「同じ文の中か」で見る。
    i = FLEET_SRC.index("_initial_job_with_unlock(")
    stmt = FLEET_SRC[i:FLEET_SRC.index("preflight_unlock:", i)]
    assert "_with_theme_memory(" in stmt, "初回ジョブ本文の組み立て地点で差し込んでいない"
    # goals そのものを差し替える経路を作らない
    assert "goals = _with_theme_memory" not in FLEET_SRC
    assert "_prime_goals_with_memory" not in FLEET_SRC


def test_priming_never_loses_the_body():
    """記憶の不調で作業内容が消えないこと。"""
    from relay.relay_fleet import _with_theme_memory
    assert _with_theme_memory("やること") .endswith("やること")
    assert _with_theme_memory("") == ""


def test_priming_is_not_applied_twice():
    from relay.relay_fleet import _MEMORY_HEADER, _with_theme_memory
    already = "%s\nx\n--- メモここまで ---\n\n本文" % _MEMORY_HEADER
    assert _with_theme_memory(already) == already


def test_local_loop_records_the_job_it_just_finished():
    assert "from relay.project_memory import record_task" in LOOP_SRC
    assert "record_task(theme_from_goal(instruction)" in LOOP_SRC


def test_local_loop_records_after_reporting_its_result():
    """記録の失敗が CLI の戻り値を変えてはならない。"""
    i = LOOP_SRC.index("print(f\"LOCAL_LOOP {job_id}: {result}\")")
    j = LOOP_SRC.index("record_task(theme_from_goal(instruction)")
    assert i < j


def test_the_dashboard_is_refreshed_without_opening_the_ui():
    assert "_refresh_selfimprove_dashboard()" in FLEET_SRC
    fn = FLEET_SRC[FLEET_SRC.index("def _refresh_selfimprove_dashboard"):]
    assert "write_json" in fn
    assert re.search(r"except Exception:\s*\n\s*pass", fn), "失敗が走行に波及しうる"


def test_the_memory_store_is_redirected_away_from_the_operators_own():
    """`.fleet/memory/*.md` はフリートが毎ゴールに前置するもの。
    テストがそこへ書くのは雑音を足すのではなく、
    **次の実走行が自分の過去について何を聞かされるか**を変える。

    見つけ方は目視: 運用者のストアに g0..g4 が数分前の日付で並び、
    実業務の62テーマと同居していた。さらに5件はテーマ名が
    記憶ヘッダーそのもので、前置された本文が新しいゴールとして
    記録されていた -- 記憶が自分を食っている。"""
    # 実行して確かめる。以前はここで conftest.py の本文を読み、`"FLEET_STATE_DIR"` の
    # 最初の一致の手前900文字に理由文があることを断言していた。別の場所に同じ名前の
    # エントリ（task_router の分類）が足された瞬間に index() がそちらを掴んで落ちた --
    # 守っている性質は何も変わらないのに。まず**効いているか**を見る。
    import os as _os
    import io as _io
    root = _os.environ.get("FLEET_STATE_DIR", "").strip()
    assert root, "テスト実行中に FLEET_STATE_DIR が設定されていない"
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    assert not _os.path.abspath(root).startswith(_os.path.abspath(repo)), (
        "FLEET_STATE_DIR が運用者のリポジトリ内を指している: %s" % root)
    # 理由が書き残されていること。どのエントリの隣かは問わない -- 場所は動く。
    src = _io.open("conftest.py", encoding="utf-8").read()
    assert "third production record" in src


def test_a_primed_body_is_not_recorded_as_a_fresh_goal():
    """記憶ヘッダーがテーマ名になった5件は、前置済みの本文を
    そのままゴールとして記録した結果。`_with_theme_memory` が
    self.goal ではなく送信本文にだけ効く、という不変条件が要る理由。"""
    from relay.relay_fleet import _MEMORY_HEADER, _with_theme_memory
    primed = _MEMORY_HEADER + "\n過去のメモ\n--- メモここまで ---\n\n本当のゴール"
    # すでにヘッダーを含む本文は二度と前置されない
    assert _with_theme_memory(primed) == primed
