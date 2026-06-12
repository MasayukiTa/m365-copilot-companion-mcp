"""Build the fleet goals file for the SWE-bench pilot: one goal per problem, pointing the agent
at its astropy worktree, with swe_check.py as the acceptance gate (DONE only when hidden tests
pass). Writes .fleet/swe/goals3.jsonl for `python -m relay.fleet_runner --goals-file ...`.
"""
import json
import os

REPO = r"C:\Users\USER\companion-mcp"
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
CHECK = os.path.join(REPO, "bench", "swe_check.py")

spec = json.load(open(os.path.join(REPO, ".fleet", "swe", "pilot_spec.json"), encoding="utf-8"))[:3]

goals = []
for t in spec:
    inst = t["instance_id"]
    wt = os.path.join(REPO, ".fleet", "swe", "work", "wt_" + inst)
    ps = t["problem_statement"]
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
        "2) 根本原因を read_file で確認。\n"
        "3) replace_in_file / write_file で**ソースのみ**を最小限に修正（テストファイルは編集しない）。\n"
        "4) 直したら DONE。隠しテストで自動検証されます。失敗なら理由が返るので、その原因を直して再度 DONE。\n"
        "解決不能と確信した場合のみ STUCK: と理由。"
    )
    goals.append({"text": text, "cwd": wt,
                  "checks": [{"type": "shell", "cmd": check_cmd, "timeout": 1300}]})

out = os.path.join(REPO, ".fleet", "swe", "goals3.jsonl")
with open(out, "w", encoding="utf-8", newline="\n") as f:
    for g in goals:
        f.write(json.dumps(g, ensure_ascii=False) + "\n")
print("wrote %s with %d goals" % (out, len(goals)))
for g in goals:
    print("  ", os.path.basename(g["cwd"]))
