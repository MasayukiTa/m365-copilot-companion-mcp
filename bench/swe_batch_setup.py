"""Reset a batch's worktrees to their clean base_commit and (re)write a goals JSONL for the
fleet -- the multi-repo generalization of swe_rerun_setup.py / swe_build_goals.py.

Each goal:
  * cwd = the instance's worktree (.fleet/swe/work/wt_<instance>)
  * text embeds the absolute wt_<instance> path  <-- REQUIRED: swe_run_until_done's RESOLVED
    detector recovers the instance id from `wt_<instance>` in the goal text.
  * checks = [shell: swe_check.py <instance> <wt>] (the official-eval acceptance gate; DONE is
    only accepted when the hidden tests pass).
The goal text is repo-agnostic (names the real library from the spec) so it works for requests,
flask, sympy, django, ... not just astropy.

  python bench/swe_batch_setup.py [--spec PATH] [--goals PATH] [--instances id ...] [--no-reset]
Defaults: spec=.fleet/swe/batch_12_spec.json, goals=.fleet/swe/goals_batch12.jsonl
"""
import argparse
import json
import os
import subprocess
import sys

REPO = r"C:\Users\USER\companion-mcp"
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
CHECK = os.path.join(REPO, "bench", "swe_check.py")
WORK = os.path.join(REPO, ".fleet", "swe", "work")


def goal_text(lib, wt, ps):
    if len(ps) > 6000:
        ps = ps[:6000] + "\n...(truncated)"
    _t = (
        "あなたは実在の Python ライブラリ **%s** のバグを修正します。\n"
        "対象リポジトリ（git チェックアウト済み・このPCのローカル）:\n"
        "  %s\n"
        "このフォルダ内のソースを、ファイルツール（grep / glob / read_file / replace_in_file / "
        "write_file）で読み込み・編集してください。ライブラリはインストール済みである必要はなく、"
        "ソースを直接直せば自動検証されます。\n\n"
        "== 修正すべき issue ==\n%s\n\n"
        "進め方:\n"
        "1) grep/glob で関連箇所を特定（issue 中のクラス名・関数名・エラーメッセージで検索）。\n"
        "2) 根本原因を read_file で確認。同じ不具合パターンがファイル内の**複数箇所**にあることが"
        "多い。1箇所直して終わりにせず、grep で全出現箇所を洗い出すこと。\n"
        "3) replace_in_file / write_file で**ソースのみ**を最小限に修正（テストファイルは編集しない）。\n"
        "4) 修正は**条件を限定**する。挙動を直すとき、設定フラグ・引数・分岐によって**元の挙動を必要とする"
        "経路**がないか確認し、無条件に消す/反転させず該当条件にゲートする。今まで通っていたテストが"
        "落ちたら、それはバグでなく**過剰修正による回帰**＝新挙動と既存挙動の両立が必要なサイン。\n"
        "5) 直したら DONE。隠しテストで自動検証され、失敗時は**実際の失敗テスト名・エラー・発生行**、"
        "および『回帰（前は通っていた）』か『未修正（対象バグが未解決）』かの区別が返るので、それを"
        "手掛かりに直して再度 DONE。安易に STUCK しないこと。\n"
        "解決不能と確信した場合のみ STUCK: と理由。"
    ) % (lib, wt, ps)
    # Generalization-direction scaffold lift (SWE_STRONG_SELFTEST=1). The 3-miss analysis (2026-06-17)
    # found the misses declared DONE off GREEN PRE-EXISTING tests that never covered the new case --
    # "N passed" carried no signal. So self-test is now RED->GREEN: write a reproducer of the issue
    # FIRST, watch it FAIL on the unpatched code, fix until it passes. This is universal TDD
    # discipline (a pre-existing suite by definition didn't catch the bug), NOT test-specialization.
    # Also lowers trust in RESEARCH (a wrong "fix unnecessary / later version" claim sank one miss).
    if os.environ.get("SWE_STRONG_SELFTEST") == "1":
        _t += ("\n\n【自己検証＝red→green を徹底】まず**編集する前に**、問題文の症状を再現する小さな"
               "スクリプト/テストを書き、未修正コードで run_python か shell_exec で実行して**実際に失敗する**"
               "ことを確認せよ（既存の通過テストは証拠にならない＝バグ発生前から通っていたはず）。その同じ"
               "再現が**通るまで**修正を続け、通ってから DONE。加えて変更箇所を覆うリポジトリ自身のテストも"
               "実行し回帰が無いことを確認する。\n"
               "**自分の再現テストを緩めて通すのは禁止**——通すためにアサーションを書き換えたく"
               "なったら、それは修正が間違っているサイン。テストではなく実装を直せ。\n")
        # Failure class 5 (fix-at-the-right-radius). Gated by SWE_FIX_RADIUS (default ON) so an
        # A/B can isolate its effect: SWE_FIX_RADIUS=0 reverts to the pre-class-5 scaffold.
        if os.environ.get("SWE_FIX_RADIUS", "1") != "0":
            _t += ("**最下層を直接叩く＝修正半径を合わせる**：候補修正を問題文の単一例が通っただけで採用するな。"
                   "その修正が支配する**最も低レベルの操作を、高レベルの入口経由でなく直接呼ぶ**再現を自作し、"
                   "さらに(a)退化・矛盾入力（空・ゼロ長・負・None・真偽値・記号的に未確定/簡約不能）と、"
                   "(b)『迂回・フォールバックで誤魔化しただけなら落ちる』対照を足し、これらも通すこと。"
                   "その挙動の正準的な定義箇所（どの層）を一行で述べ、編集がその層にあるか確認し、上流のdispatchが"
                   "壊れた経路を避けているせいで再現が通っているだけなら修正をプリミティブまで下げよ。\n")
        _t += ("【調査結果は鵜呑みにしない】RESEARCH の結論は手掛かりであって絶対ではない。特に『修正は不要』"
               "『後のバージョンの話』という主張は、実際のチェックアウト済みコードと再現結果に照らして確かめ、"
               "食い違えばコードと再現を信じよ。")
    # Minimal-AND-COMPLETE diff (SWE_MINIMALITY=1). Over-engineering is one failure; the 3-miss
    # analysis found the OPPOSITE dominates auto -- a diff too SMALL / off-target (1 of 2 hunks;
    # producer fixed, consumers not; wrong root cause). So pair minimality with COMPLETENESS:
    # producer<->consumer, method-family symmetry, every occurrence. General engineering discipline.
    if os.environ.get("SWE_MINIMALITY") == "1":
        _t += ("\n\n【最小かつ完全な差分】正しい修正は通常ごく小さい（多くは数行）。**根本原因に的確に当たる"
               "最小の差分**だけを書け（余計なリファクタ・防御的コード・無関係な整形は過剰修正の元）。"
               "ただし**小さすぎ・的外れも同じく欠陥**——最小性と引き換えに**完全性**を落とすな:\n"
               "・データ契約（キー名・戻り値・レコード形）を変えたら**書く側と読む側の両方**を直す"
               "（producer だけ直して consumer を放置しない）。\n"
               "・あるメソッド/変換/分岐を足したら**対になる方向**（例 `X→Y` を足したら `Y→X` も）が"
               "同じ変更を要しないか確認する。\n"
               "・状態を変える（mutate する）箇所を直したら、その状態を**コピー/clone する箇所**も同じ"
               "更新を要しないか（古い値をキャッシュ/共有して持ち回っていないか）、同じバグを持つ"
               "**兄弟メソッド**が無いかを確認する。\n"
               "・同じ不具合パターンがファイル内の複数箇所にあれば grep で全て直す。\n"
               "【症状の出口でなく源を直す】症状が**現れる**呼び出し側（テンプレートタグ・再帰の入口・"
               "ユーザ向け API）ではなく、症状が**通過する共有の定義**（設定プロパティ・正規化関数・"
               "dispatch/eval 経路）を直せ。直す前に、その記号が本当に問題の経路で使われている方かを"
               "症状→源へ辿って確かめる（caller でなく callee を直す）。\n"
               "【握り潰す vs 正しく表出する】『例外を消す』のと『続行しつつ正しい診断を出す』は別物。"
               "要求が警告・メッセージ・戻り値の**表出**なら、それを**出す**修正にせよ——契約が"
               "「ダウングレードして残す」診断を求めているのに黙って抑制すると落ちる。\n"
               "『最小か？』と『症状は完全に消え、関連する全箇所を直したか？』の両方を自問せよ。")
    return _t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=os.path.join(REPO, ".fleet", "swe", "batch_12_spec.json"))
    ap.add_argument("--goals", default=os.path.join(REPO, ".fleet", "swe", "goals_batch12.jsonl"))
    ap.add_argument("--instances", nargs="*", default=None)
    ap.add_argument("--no-reset", action="store_true",
                    help="write goals only; do not git-reset the worktrees")
    args = ap.parse_args()

    spec = {s["instance_id"]: s for s in json.load(open(args.spec, encoding="utf-8"))}
    insts = args.instances or list(spec.keys())

    goals = []
    for inst in insts:
        s = spec.get(inst)
        if not s:
            print("SKIP unknown instance", inst); continue
        wt = os.path.join(WORK, "wt_" + inst)
        if not os.path.isdir(wt):
            print("SKIP missing worktree", inst, "(run swe_repos_setup_batch.py first)")
            continue
        if not args.no_reset:
            subprocess.run(["git", "-C", wt, "checkout", "--", "."], capture_output=True)
            subprocess.run(["git", "-C", wt, "clean", "-fd"], capture_output=True)
            d = subprocess.run(["git", "-C", wt, "diff"], capture_output=True, text=True)
            print("reset %-42s diff_chars=%d" % (inst, len(d.stdout)))
        lib = s["repo"].split("/")[-1]
        check_cmd = '"%s" "%s" %s "%s"' % (VENVPY, CHECK, inst, wt)
        # acceptance timeout must exceed swe_check's WORST-CASE wall time so the acceptance
        # layer never kills swe_check (python) mid-eval -- that would leave the detached docker
        # eval container running (a leak one layer above #17). swe_check worst case ~=
        # wsl eval(1200) + cat report(60) + docker cleanup(120) = ~1380s; 1300 was BELOW that,
        # so a slow-but-legitimate eval got pre-empted. 1500 sits above 1380 and matches the
        # watchdog's EVAL_STALL_CEILING_S (relay_fleet.py), keeping the two ceilings consistent.
        goals.append({"text": goal_text(lib, wt, s["problem_statement"]), "cwd": wt,
                      "checks": [{"type": "shell", "cmd": check_cmd, "timeout": 1500}]})

    with open(args.goals, "w", encoding="utf-8", newline="\n") as f:
        for g in goals:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    print("wrote %s with %d goal(s)" % (args.goals, len(goals)))
    # sanity: every goal text must contain its wt_<instance> token (RESOLVED detection contract)
    bad = [g for g in goals if ("wt_" not in g["text"])]
    if bad:
        print("WARNING: %d goal(s) missing wt_ token in text!" % len(bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
