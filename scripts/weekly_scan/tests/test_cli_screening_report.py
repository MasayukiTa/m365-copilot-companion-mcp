# -*- coding: utf-8 -*-
"""単体試験: CLI が (c) 新規スクリーニングと (d) 報告を配線していることを検証する。

合成データ・一時ファイルのみ。判定器・DBは呼ばない(--dry-run)。このテストは
レビュア指摘「cli 本体に screening/report が一切出現せず (c)(d) が未接続」に
対する回帰ガードである。
"""
import json
import os

from scripts.weekly_scan import cli


def _screen_inputs_strong():
    """3品名すべてで大きく一貫した効果、交絡なし -> strong になりやすい。"""
    rows = []
    for product in ("A", "B", "C"):
        for i in range(6):
            rows.append({"lot": "t%s%d" % (product, i), "product": product,
                         "line": "L1", "date": "2026-08-01",
                         "exposure": "treated", "value": 2.0})
            rows.append({"lot": "c%s%d" % (product, i), "product": product,
                         "line": "L1", "date": "2026-08-01",
                         "exposure": "control", "value": 1.0})
    return [{"result_item": "ピール値", "rows": rows, "min_lots_per_group": 5}]


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def test_cli_module_wires_screening_and_report():
    """cli モジュールが screening/report の公開関数を実際に import ・配線している。"""
    assert hasattr(cli, "screen_result_item")
    assert hasattr(cli, "preregister_strong")
    assert hasattr(cli, "write_md")
    assert hasattr(cli, "append_tracking")
    assert callable(cli._run_screening_and_report)
    assert callable(cli._candidate_to_detection)


def test_cli_dry_run_screens_and_reports(tmp_path, capsys):
    """--screen-input + --report-out で (c)(d) が走り、強い候補が報告化される。
    --dry-run なので判定器・DBは触らず、実ファイルには書かない。"""
    ledger = os.path.join(str(tmp_path), "台帳.json")
    _write_json(ledger, [
        {"analysis_id": "aid_b", "class": "B", "dir": "d1", "kshort": 2, "add80": 1,
         "last_scanned": "2026-10-01", "next_trigger": "t"},
    ])
    screen_in = os.path.join(str(tmp_path), "screen.json")
    _write_json(screen_in, _screen_inputs_strong())
    report_out = os.path.join(str(tmp_path), "reports")
    prereg_out = os.path.join(str(tmp_path), "prereg.jsonl")
    track = os.path.join(str(tmp_path), "track.json")

    rc = cli.main([
        "--ledger", ledger, "--dry-run",
        "--screen-input", screen_in,
        "--report-out", report_out,
        "--prereg-out", prereg_out,
        "--track-ledger", track,
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(c) screened=1 strong=1" in out
    assert "(d) reports=1" in out
    # --dry-run なので実ファイル(prereg/report/track)は生成されない(一時に逃がす)。
    assert not os.path.exists(prereg_out)
    assert not os.path.exists(report_out)
    assert not os.path.exists(track)


def test_cli_real_screens_writes_report_and_prereg(tmp_path, capsys):
    """dry-run でない場合、--screen-input 経路は報告 md ・事前登録・追跡台帳を実際に書く。
    (a)(b) は --wiring が無いので判定器に進まないよう B/C/D 行は置かず、(c)(d) のみ検証。
    判定器は (c)(d) ではそもそも呼ばれない(スクリーニングは記述統計のみ)。"""
    ledger = os.path.join(str(tmp_path), "台帳.json")
    _write_json(ledger, [
        {"analysis_id": "aid_a", "class": "A", "dir": "d1",
         "last_scanned": "2026-10-01", "next_trigger": "t"},
    ])
    screen_in = os.path.join(str(tmp_path), "screen.json")
    _write_json(screen_in, _screen_inputs_strong())
    report_out = os.path.join(str(tmp_path), "reports")
    prereg_out = os.path.join(str(tmp_path), "prereg.jsonl")
    track = os.path.join(str(tmp_path), "track.json")

    # dry-run なしだが --wiring も無い。(a)(b) は build_ports で SystemExit になるので、
    # ここでは (c)(d) の実書き出しを _run_screening_and_report で直接検証する。
    screened, strong, reports = cli._run_screening_and_report(
        screen_input_path=screen_in, prereg_out=prereg_out,
        report_out=report_out, track_ledger=track,
        scanned_on="2026-11-04", dry_run=False,
    )
    assert (screened, strong, reports) == (1, 1, 1)
    # 実ファイルが生成される。
    assert os.path.isfile(prereg_out)
    prereg_lines = [l for l in open(prereg_out, encoding="utf-8").read().splitlines() if l.strip()]
    assert len(prereg_lines) == 1
    rec = json.loads(prereg_lines[0])
    # 順序の信頼時計を偽装しない: order_clock は registered/initial のみ。
    assert rec["order_clock"] == {"source": "registered", "state": "initial"}
    # 報告 md ができている。
    mds = [f for f in os.listdir(report_out) if f.endswith(".md")]
    assert len(mds) == 1
    body = open(os.path.join(report_out, mds[0]), encoding="utf-8").read()
    assert "週次走査 検出報告" in body
    assert "ピール値" in body
    # 追跡台帳が書かれ、分類 C。
    track_data = json.load(open(track, encoding="utf-8"))
    assert len(track_data) == 1
    assert track_data[0]["class"] == "C"


def test_cli_without_screen_input_runs_only_ab(tmp_path, capsys):
    """--screen-input 省略時は従来どおり (a)(b) のみ。screened=0 strong=0 reports=0。"""
    ledger = os.path.join(str(tmp_path), "台帳.json")
    _write_json(ledger, [
        {"analysis_id": "aid_b", "class": "B", "dir": "d1", "kshort": 2, "add80": 1,
         "last_scanned": "2026-10-01", "next_trigger": "t"},
    ])
    rc = cli.main(["--ledger", ledger, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(c) screened=0 strong=0" in out
    assert "(d) reports=0" in out
