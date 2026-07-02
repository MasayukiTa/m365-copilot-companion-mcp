"""Reset the two unresolved pilot worktrees to their clean base_commit and write goals2.jsonl
(same goal text + acceptance gate as the original build) for an honest from-scratch re-run with
the improved feedback gate. Pass instance ids as argv, or default to the two that failed."""
import json, os, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
CHECK = os.path.join(REPO, "bench", "swe_check.py")

INSTS = sys.argv[1:] or ["astropy__astropy-14182", "astropy__astropy-14365"]

spec = {t["instance_id"]: t for t in
        json.load(open(os.path.join(REPO, ".fleet", "swe", "pilot_spec.json"), encoding="utf-8"))}

goals = []
for inst in INSTS:
    wt = os.path.join(REPO, ".fleet", "swe", "work", "wt_" + inst)
    # reset to clean base_commit
    subprocess.run(["git", "-C", wt, "checkout", "--", "."])
    subprocess.run(["git", "-C", wt, "clean", "-fd"], capture_output=True)
    d = subprocess.run(["git", "-C", wt, "diff"], capture_output=True, text=True)
    print("reset", inst, "-> diff_chars now", len(d.stdout))
    ps = spec[inst]["problem_statement"]
    if len(ps) > 6000:
        ps = ps[:6000] + "\n...(truncated)"
    check_cmd = '"%s" "%s" %s "%s"' % (VENVPY, CHECK, inst, wt)
    text = (
        "あなたは実在の Python ライブラリ **astropy** のバグを修正します。\n"
        "対象リポジトリ（git チェックアウト済み・このPCのローカル）:\n"
        "  " + wt + "\n"
        "このフォルダ内のソースを、ファイルツール（grep / glob / read_file / replace_in_file / write_file）で"
        "読み込み・編集してください。astropy はインストール済みである必要はなく、ソースを直接直せば自動検証されます。\n\n"
        "== 修正すべき issue ==\n" + ps + "\n\n"
        "進め方:\n"
        "1) grep/glob で関連箇所を特定（issue 中のクラス名・関数名・エラーメッセージで検索）。\n"
        "2) 根本原因を read_file で確認。同じ不具合パターンがファイル内の**複数箇所**にあることが多い。"
        "1箇所直して終わりにせず、grep で全出現箇所を洗い出すこと。\n"
        "3) replace_in_file / write_file で**ソースのみ**を最小限に修正（テストファイルは編集しない）。\n"
        "4) 直したら DONE。隠しテストで自動検証され、失敗時は**実際の失敗テスト名・エラー・発生行**が返るので、"
        "それを手掛かりに残りの原因を直して再度 DONE。安易に STUCK しないこと。\n"
        "解決不能と確信した場合のみ STUCK: と理由。"
    )
    goals.append({"text": text, "cwd": wt,
                  "checks": [{"type": "shell", "cmd": check_cmd, "timeout": 1300}]})

out = os.path.join(REPO, ".fleet", "swe", "goals2.jsonl")
with open(out, "w", encoding="utf-8", newline="\n") as f:
    for g in goals:
        f.write(json.dumps(g, ensure_ascii=False) + "\n")
print("wrote", out, "with", len(goals), "goals")
