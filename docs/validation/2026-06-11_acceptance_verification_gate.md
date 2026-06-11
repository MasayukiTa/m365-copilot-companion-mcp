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

## 追補 2026-06-12: ライブE2E実証 ＋ ツール使用の可観測性

### (1) ライブE2E実証（実機の Copilot 相手にゲートが発火）
ユニット/結合だけでなく、**実機の M365 Copilot impl エージェント**にゲート付きゴールを1本流して
end-to-end を実証した。
- ゴール: 「gate_live_demo/result.txt に 1〜20 の二乗和(=2870)だけを run_python で計算し
  write_file で保存」。チェック: `file_contains result.txt "2870"`（枠がディスク上の実ファイルを
  独立検証）。
- 1回目は**環境要因で失敗**: アイドル放置の headless Edge が新規タブ生成でハング→内蔵
  watchdog が 150s で hard-reset→タブクローズ→`TargetClosedError`→worker ERROR(turn 0)。
  ゲートには未到達（=実装ではなく Edge 劣化）。空きRAM ~2.2GB と低かった。
- hard-reset 後の Edge は健全化（新規タブ composer 描画 10.6s を実測で確認）。**再試行で成功**:
  worker 進行 t1(実行中)→t2→**検証中(verifying)**→DONE、**89.2s / 2ターン**、`result.txt = 2870`。
  チェック付きゴールで DONE に到達できるのは全チェック PASS 時のみ＝**ライブで地上検証が効いた証拠**。
- 既知の小欠落を修正: 最終スナップショットが `verified`/`verify_attempts` を欠いていた
  （`run_relay_fleet` の返却と fleet_runner 最終 snapshot に追加）。

### (2) ツール使用の可観測性（Copilot の MCP 呼び出しを記録）
relay が Copilot を駆動する際、エージェントの MCP ツール呼び出し(read_file/write_file/run_python…)は
**チャットDOMに出ず**「何を編集/実行したか」が見えない（Claude Code は標準で見せる）。これを
サーバ側で記録可能にした。
- 新規 `tools/trace_ops.py`: `wrap_for_trace(fn)` が**シグネチャ・型注釈・名前・docstring を完全保存**
  したラッパで各呼び出しを `.companion_runs/toolcalls_YYYY-MM-DD.jsonl` に追記(ts/name/ok/dur_ms/
  args(束縛名付・truncate)/result/error)。`toolcalls_tail(n)` で読み出し（MCPツール化）。
- **既定 OFF**: 環境変数 `MCP_TRACE_TOOLCALLS` 未設定なら `wrap_for_trace` は fn をそのまま返す
  ＝挙動ゼロ変更。`register()` に配線済だが OFF では無影響。
- **安全性の決定的検証**: `main.py` を **tracing OFF と ON 両方**でロード→FastMCP が **138 ツール
  全部を公開**（`write_file` 等ラップ対象含む）。ラッパがスキーマ生成を壊さないことを実機ビルドで確認。
  稼働中サーバは無傷（次回 restart で取り込み、フラグ ON にして初めて記録開始）。
- テスト `tools/test_trace.py` **11/11**（OFF時は同一関数オブジェクト返却／ON時シグネチャ完全一致／
  束縛名付き記録／例外も記録し再送出／長大引数 truncate）。

合計テストは **58/58**（acceptance 18・fleet-verify 12・folder-verify 8・relay-loop 9・trace 11）。

### (3) 意味的チェック: import_smoke（構文を超える）
`acceptance.py` に `import_smoke` 種別を追加。py_compile（構文のみ）を超えて**実際に import**し、
読込時エラー（モジュールスコープの未定義名・壊れた import・初期化失敗）を捕捉する。folder_coder に
`--import-smoke`（opt-in）。**注: import は副作用があり、特殊コンテキストを要するモジュールでは
false-FAIL し得るため `--verify` には束ねず明示 opt-in**。真の意味的正しさは `--check-cmd "pytest…"`。
テスト追加で acceptance は **20/20**（全体 **60/60**）。

### 残課題（追補）
- attach 中に Edge が hard-reset されると worker が**終端 ERROR**になりゴールが失われる（上記1回目）。
  回復可能扱い（pending 戻し or FleetContextLost 化）にする堅牢化は別途。
- tracing の**実ライブ記録**はサーバを `MCP_TRACE_TOOLCALLS=1` で再起動して初めて出る（本追補では
  スキーマ無破壊までを実機確認、実記録はユニットで確認）。

## 限界・残（正直に）

- ゲートはユーザ（オペレータ）がチェックを与えたゴールにだけ効く。チェック無しゴールは
  従来どおり自己申告 DONE を信頼する。folder_coder `--verify` は Python 構文（py_compile）
  までを自動付与；意味的正しさは `--check-cmd` でプロジェクトのテストを指定して初めて担保。
- チェックは別プロセスで走るためフリートは固まらないが、極端に長いビルドは当該ワーカーの
  検証が長引く（タイムアウトで打ち切り）。
- `verify_ops.py`（既存 MCP 検証ツール）とは別系統。ゲートは枠側で `acceptance.py` を直接
  使う（`require_unlocked` ゲートに触れない＝枠はローカルの信頼主体、spec §1）。
