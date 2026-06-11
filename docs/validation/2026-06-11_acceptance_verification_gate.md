# 受け入れ検証ゲート（spec §3-③ 検証ループのループ結線）

2026-06-11 / m365-copilot-companion-mcp

## 目的（なぜやったか）

「Claude Code としての性能を詰める」要求への中核実装。従来、自律ループは Copilot が
最終行に書く `DONE` を**そのまま信じて終了**していた（`relay_fleet.RelayWorker._decide`
／`copilot_autopilot_relay.run_relay`）。spec §3-③ と §8 手順2が説く「枠が Copilot の
自己申告を信じず、ツール層を直接叩いて地上の真実を観測→食い違えば訂正を再注入」という
**検証ループがループ本体に未結線**だった。`verify_ops.py` の地上検証群は存在したが、
Copilot 側からしか呼べず、枠（ループ）の受け入れゲートとして機能していなかった。

これが Claude Code との信頼性差の本体。Claude Code が信頼できるのは「テスト/ビルドを
自分で回し、通るまで done と言わない」から。本変更でその閉ループを入れた。

## 何を入れたか

### 新規: `relay/acceptance.py`（検証ランナー、stdlib のみ）
機械検証可能なチェックを**ローカルで実際に実行**し `(passed, detail)` を返す。
- チェック種別: `shell`（argv/cmd、`expect_code`/`expect_stdout`）, `pytest`,
  `python`, `py_compile`（構文チェック）, `file_exists`, `file_contains`（substr/regex）。
- **ノンブロッキング**: `Check(spec, cwd).start()` → `poll()` が実行中は `None`、完了で
  `(passed, detail)`。別 OS プロセスが走り、`poll()` は `proc.poll()` だけ見るので、
  フリートの単一スレッド・ラウンドロビンを固めない（他ワーカーは進み続ける）。
- タイムアウトで kill→fail。失敗時は stderr/stdout 末尾を `detail` に載せる（再注入用）。
- 信頼モデル: チェックはゴールと同じ権限で動く＝**ローカルのオペレータ（goalsファイル／
  folder_coder／コックピット）由来のみ**。Copilot（オラクル）の出力からチェックを組まない。

### 結線: 両ループに受け入れゲート
- `relay_fleet.RelayWorker`: ゴールが `checks` を持つ場合、`DONE` で即終了せず新状態
  **`verifying`** へ。枠がチェックを順に実行 → 全PASSで真の `DONE`(`verified=True`)。
  いずれか FAIL なら**実際の出力を `VERIFY_FIX_JOB` で再注入**し作業継続。
  `max_verify_attempts`(既定3)到達で `stuck`/outcome `VERIFY_FAILED`。
  チェック無しのゴールは従来どおり `DONE` 受理（`verified=False`）＝**後方互換**。
- `copilot_autopilot_relay.run_relay`: 同等のゲートを `checks`/`cwd`/`max_verify_attempts`
  引数＋`run_all_blocking` で実装（単一relayはブロッキング）。CLI に `--check`(JSON)/
  `--check-cwd` を追加。
- 共有文言 `VERIFY_FIX_JOB`: 「あなたは DONE と報告したが自動検証で不合格。実際の検証結果は
  …」と**地上の真実**を渡して修正させる（曖昧な「間違いかも」ではない）。
- `PROTOCOL` に「DONE と書く前に自分でも実行確認し、こちらでも自動検証する」を明記＝
  agent 自身の自己検証を促しゲート収束を速める。

### ゴールが検証基準を運ぶ
- ゴールは「素の文字列（チェック無し＝従来）」または `{"text","checks","cwd"}` の dict。
  `relay_fleet.goal_fields` が正規化。
- `fleet_runner._read_goals`: goalsファイルの行頭が `{` なら JSON としてパース（不正なら
  素の行として扱う）。dict ゴールでもスナップショット/結果キー/中断再開が壊れないよう
  `gtexts`（表示テキスト）で扱い、`_unfinished()` は checks 込み dict を返す（再開で
  ゲートが失われない）。
- `folder_coder`: `--verify` で per-file の Python 編集ゴールに `py_compile` チェックを
  自動付与（編集でファイルが壊れていないことを枠が証明）。`--check-cmd "python -m pytest -q"`
  で全編集ゴールに shell チェックを付与。チェック付きゴールは JSON 行で出力。
- コックピット連携: `status.json` に `verified`/`verify_attempts` を追加、pill に
  `verifying`=「検証中」、outcome `VERIFY_FAILED`→status `stuck` マッピング。

## 検証（すべて実機・ブラウザ無しで決定的に実行）

| テスト | 内容 | 結果 |
|---|---|---|
| `relay/test_acceptance.py` | shell exit/expect_stdout・py_compile good/bad・file_exists/contains・timeout kill・ノンブロッキング poll 契約・run_all 先頭失敗停止 | **18/18 PASS** |
| `relay/test_fleet_verify.py` | チェック無し=DONE信頼／PASS→verified／FAIL→真実再注入→ready／上限で VERIFY_FAILED／複数チェックは先頭失敗で停止／全PASS／goal_fields | **12/12 PASS** |
| `relay/test_folder_verify.py` | folder_coder --verify→goalsファイル→_read_goals→goal_fields→実ファイル compile の一気通し。ファイルを壊すと同じゲートが FAIL | **8/8 PASS** |
| `relay/test_relay_loop.py` | 既存7シナリオ＋単一relayゲート2ケース（verify_pass／verify_fail_cap） | **9/9 PASS** |
| import smoke | fleet_runner / relay_fleet / copilot_autopilot_relay / folder_coder / acceptance | **OK** |

合計 **47/47**。既存の自律ループ挙動（DONE/STUCK/FAIL→fix/timeout/maxturns/kill-switch）は
無回帰。

## 使い方（例）

```powershell
# フォルダに「型ヒント追加」、各 .py 編集後に compile が通ることを枠が検証してから DONE
.\.venv\Scripts\python.exe -m relay.folder_coder --folder C:\proj `
    --instruction "型ヒントを追加" --mode per-file --verify
.\.venv\Scripts\python.exe -m relay.fleet_runner --goals-file "C:\proj\.fleet_goals.txt"

# プロジェクトのテストが実際に通ることを DONE の条件にする
.\.venv\Scripts\python.exe -m relay.folder_coder --folder C:\proj `
    --instruction "バグ修正" --mode single --check-cmd "python -m pytest -q"

# 単一relayをCLIで検証付き
.\.venv\Scripts\python.exe relay\copilot_autopilot_relay.py --conversation-url <URL> `
    --goal "..." --check "{\"type\":\"shell\",\"cmd\":\"python -m pytest -q\"}"
```

## 限界・残（正直に）

- ゲートはユーザ（オペレータ）がチェックを与えたゴールにだけ効く。チェック無しゴールは
  従来どおり自己申告 DONE を信頼する。folder_coder `--verify` は Python 構文（py_compile）
  までを自動付与；意味的正しさは `--check-cmd` でプロジェクトのテストを指定して初めて担保。
- チェックは別プロセスで走るためフリートは固まらないが、極端に長いビルドは当該ワーカーの
  検証が長引く（タイムアウトで打ち切り）。
- `verify_ops.py`（既存 MCP 検証ツール）とは別系統。ゲートは枠側で `acceptance.py` を直接
  使う（`require_unlocked` ゲートに触れない＝枠はローカルの信頼主体、spec §1）。
