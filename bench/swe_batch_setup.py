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
    # Generalization-direction scaffold lift (SWE_STRONG_SELFTEST=1): the clean single-shot
    # misses are "right file + right approach, but the exact patch is a step off". Have the agent
    # self-verify against the REPO'S OWN tests (not the hidden grading tests) before DONE so it
    # catches its own imprecision in one shot. This is general solving discipline, NOT
    # test-specialization. A/B'd separately so it never confounds the research on/off axis.
    if os.environ.get("SWE_STRONG_SELFTEST") == "1":
        _t += ("\n\n【自己検証の強化】DONE の前に必ず、変更したコードを覆う**このリポジトリ自身のテスト**を "
               "grep でテスト関数名/ファイルを特定し、run_python か shell_exec で実際に実行して"
               "**通ることを確認**してから DONE。テストが見つからなければ issue の再現コードを自分で書いて"
               "期待動作を確かめよ。隠しテストではなく自分で回せるテストで裏取りする"
               "（特定テストへの最適化ではなく、一般的な自己検証の徹底）。")
    # Minimal-diff bias (SWE_MINIMALITY=1). The dominant miss is OVER-ENGINEERING: a 40+ line diff
    # where the correct fix is 2-7 lines. Tell the agent to make the SMALLEST change that fixes the
    # root cause -- general engineering discipline, not test-gaming.
    if os.environ.get("SWE_MINIMALITY") == "1":
        _t += ("\n\n【最小差分の原則】正しい修正は通常ごく小さい（多くは数行・1ファイル）。**根本原因に"
               "的確に当たる最小の差分**だけを書け。動くからといって余計なリファクタ・防御的コード・"
               "別ファイルの変更・無関係な整形を加えるな（過剰修正は回帰やテスト失敗の元）。"
               "『この変更は本当に必要か？』を各行で自問し、不要なら削れ。")
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
