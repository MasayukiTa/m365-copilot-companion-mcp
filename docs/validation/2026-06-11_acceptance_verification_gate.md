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

## 追補 2026-06-12 (2): 自然言語フロントドア・自動検証検出・反証器(operator B)

**動機（ユーザー指摘）**: 「Claude Code は goals ファイルや --verify フラグなんて書かない。自然言語で
言えばやってくれる」。実際コックピットの「フォルダ→ゴール」は `--mode per-file`（ファイル1個=ゴール1個）
＋検証なしで、「バグ直して」のような横断タスクにゴールが洪水のように出て無意味だった（=「動かん」の正体）。

### (4) 自動検証検出 `relay/project_introspect.py`
フォルダを見て**検証方法を自分で決める**: pytest 設定/`test_*.py`/`tests/` があり pytest が
インストール済なら `pytest -q`、テストはあるが pytest 未導入なら compile にフォールバック＋注記、
テストが無く .py だけなら `compileall`（構文ゲート）、`package.json` の test script があれば `npm test`。
`--check-cmd` を書かずに検証が自動で付く。

### (5) 自然言語フロントドア `relay/code_task.py`
`code_task -i "落ちてるテストを直して" -f C:\proj` だけで、検証を自動付与した**1タスク**として実行
（per-file 洪水ではない）。エージェントが必要な所を自分で探して編集、枠はプロジェクトの**実テスト/コンパイル
が実際に通るまで DONE にしない**。コックピット③（FolderToGoals）も per-file 廃止→ code_task 直接起動に
置換（csc コンパイル確認済。実行中 exe はロックされるので反映には閉じて build_cockpit.bat 再実行）。

### (6) 反証器 operator B `relay/refuter.py`（spec §4B）
機械検証で捕まらない意味的誤り（ゴール誤読・エッジケース無視・症状隠し・要件を実際は検証しないテスト）対策。
DONE 候補に対し**独立した第2の Copilot 会話**を side-page で開き「達成できていない具体的欠陥を探せ→
REFUTED:<理由> か UPHELD」。REFUTED なら実装側に再注入。**既定OFF・予算上限**（`--refuter --max-refute N`、
spec §4B のコスト2倍を考慮）。run_relay に結線。UNCLEAR はループを縛らないため「通す」扱い。

### ライブ実証（自然言語コーディングが実機で通った）
バグ入りミニプロジェクト（`calc.py: return a-b`＝バグ、`test_calc.py: assert add(2,3)==5` が失敗）を作り、
`code_task -i "calc.py のバグを直して pytest が通る状態にして" -f <proj>` を実機 Copilot に流した:
- 自動で **pytest 検証を選定** → エージェントが `calc.py` を `a+b` に修正 → 枠が**実 pytest 実行→1 passed**
  → **outcome DONE / verified True /「acceptance verified (1 check(s))」/ 3ターン 131s**。
- goals ファイル・フラグ・per-file 指定なし。**= Claude Code 体験（自然言語 in、検証済み work out）を実機実証**。
- 副次修正: **pytest が venv 未導入だった**（テスト検証が環境エラーで落ちる「動かん」要因）→ `pip install pytest`
  ＋ requirements.txt に追加。detect は pytest が import 可能なときだけ pytest を採用するよう堅牢化。

テスト: project_introspect/code_task 12・refuter 10 を追加。**全体 82/82**
（acceptance 20・fleet-verify 12・folder-verify 8・relay-loop 9・code-task 12・refuter 10・trace 11）。

## 追補 2026-06-12 (3): 反証器のフリート結線 ＋ attach 堅牢化

追補2の2つの残課題を解消した。

### 反証器をフリート（並列の主UX）へ — 非ブロッキング化
フリートは単一スレッドのラウンドロビンなので、side-page の反証を同期で待つと他ワーカーを数分固める。
`refuter.RefuterSession`（ノンブロッキング: `start()` で側チャットを開き反証プロンプト送信＝一度きりの短い操作、
`poll()` が判定確定まで None を返す。worker 自身の send/wait と同型）を追加。`RelayWorker` に `refuting` 状態を
追加し、機械チェック通過（またはチェック無し）で候補DONE→予算内なら反証セッション開始→REFUTED なら理由を
`REFUTE_FIX_JOB` で再注入し継続、UPHELD/UNCLEAR なら受理。`fleet_runner --refuter/--max-refute`、
`code_task --refuter` で利用可。pill「反証中」。**これで自然言語 code_task でも反証が効く**（既定OFF・予算上限）。

### attach 堅牢化（追補1の残）
attach 失敗時、`context.cookies()` を probe して**Edge 全体が死んでいれば `FleetContextLost` を送出**→runner が
再接続して未完ゴール（当該ゴール含む）を再開。コンテキストが生きている単発の開通失敗は従来どおり worker ERROR。
これで「watchdog の hard-reset が attach に当たって worker 終端ERROR→ゴール消失」が解消。

### テスト（追補3）
`relay/test_fleet_refute.py` **10/10**: 候補DONE→UPHELD→done／REFUTED→理由再注入→ready／予算上限で受理／
checks＋反証の合成（verify pass→refuting→done verified）／非ブロッキング（数回 poll で確定）／反証OFFで即done／
**Edge全体死亡→FleetContextLost（ゴール再開可）**／生存コンテキストの単発失敗→ERROR で run 完走。

合計 **92/92**（acceptance 20・fleet-verify 12・folder-verify 8・relay-loop 9・code-task 12・refuter 10・
fleet-refute 10・trace 11）。`main.py`（サーバ全ツール）もビルド成功。

## 追補 2026-06-12 (4): フリート反証の実機ライブ実証 ＋ 観測由来の堅牢化3点

`code_task --refuter` を実機 Copilot に複数回流して反証を実地検証し、その過程で見えた信頼性問題を潰した。

### 実機で確立したこと
- **フリート反証が end-to-end でライブ動作**: 2回の完走 run（113s／146.8s）で **outcome DONE・verified True・
  refuter#1 発火**。非ブロッキング `RefuterSession`＋worker `refuting`状態＋ループ処理が実機エージェント相手に機能。
- **クリーンな判定（UPHELD）をライブ取得**: 直接経路 `run_refuter`（nudge 込み）で、正しい実装に対し独立レビュアーが
  実際にレビュー→**UPHELD** を返すことを実証。

### 観測由来の堅牢化（すべてテスト緑・コミット）
1. **send 信頼性**: 低RAM(~2.4GB)下で M365 composer の submit が遅く「composer still holds text after 3 attempts」
   で turn1 STUCK が頻発。`send()` の空待ち窓を 6s→12s に拡張＋**約1秒毎に再arm した送信ボタンを再クリック**
   （load 起因の no-op クリック対策）。改善後この送信失敗は解消（次 run は別要因に変化）。成功送信には無影響・
   二重送信も減る。
2. **反証 nudge**: レビュアーは初回「ファイルとテストを確認します」と**前置きだけ返し判定に到達しない**（実装者の
   CONTINUE と同じ）。プロンプトで「前置きで終わらせず即判定」を強制＋UNCLEAR なら判定を促す nudge を最大2回
   （run_refuter／RefuterSession 両方）。これで UNCLEAR→**UPHELD**（直接経路で実証）。
3. **STUCK 誤検知**: `"STUCK" 部分文字列`マッチは、エージェントが単に語に言及しただけで完走を誤って中断していた。
   `reported_stuck()`＝**`STUCK:`/`STUCK：` マーカー必須**に厳格化（両ループ）。

### 正直な残（環境側）
- 低RAM(~2.4GB空き)で M365 SPA composer が遅く、send は緩和済だが完全には安定しない。
- エージェントが自明タスクでも**本物の STUCK: を返す**ことがある（MCP サーバはローカル稼働=PID:8000 LISTENING 確認済
  だが、ツール利用/応答の実機安定性は変動）。フリート反証判定も負荷時に UNCLEAR になり得る（ループは機械検証済
  DONE を安全に受理）。**安定運用には空きRAM確保／Edge プロファイル再作成が有効**。
- 反証の実機判定品質（UPHELD/REFUTED を毎回クリーンに）は nudge で改善したが、負荷時はなお UNCLEAR の余地。

## 追補 2026-06-12 (5): リポマップ・プライミング（catch-up）＋ 多視点レビューパネル（差別化）

### catch-up: コードベース理解 `relay/repo_map.py`
フォルダを stdlib `ast` で解析し「ファイルツリー＋各 Python の top-level def/class のシグネチャ＋docstring
1行」を ~4KB に圧縮した地図を生成。`code_task` が**ゴール冒頭にこの地図を注入**＝エージェントが盲目的 grep
でなく Claude Code/aider 同様に**地図を持って着手**。既定ON（`--no-map` で無効）。dry-run で relay/ の
3916字マップ生成を確認（全関数シグネチャ＋docstring が正確に並ぶ）。test_repo_map 9/9。

### 差別化: 多視点レビューパネル（Claude Code に無い）
反証を観点多様化。全レビュープロンプトが**①正しさ ②境界値・エラー処理 ③セキュリティ**の3観点を当てる
（無コスト強化、両経路に適用）。厳密版として単一relay経路に `--panel`: **3観点の独立レビュアー×多数決**
（`aggregate_panel`＝過半数 REFUTED で初めて差し戻し。少数の過敏な指摘では縛らない）。フリート/code_task は
多視点化した単一レビュアー（パネルの N 独立セッションはコスト/状態増のため単一relay限定、フリート版は将来）。
test_refuter 15/15（集約・観点プロンプト・パネル統合）。

全体 **106/106**、`main.py` ビルド成功。

## 追補 2026-06-12 (6): プラン提示UX（catch-up）＋ レビューパネルのフリート展開（差別化）

**前提訂正（ユーザー）**: impl エージェントの中身は **Opus 4.8（＝Claude 本体と同じ）**。よって「生の知能差」
という構造的天井は存在しない。残る差は **UI 駆動の信頼性とUX/機能の充足のみ**＝原理的に Claude Code 近傍まで到達可。

### catch-up: プラン提示→承認→実行（Claude Code の看板ループ）`relay/planner.py`
既存の steering(割り込み)チャネルの上に「計画フェーズ」を追加。`--plan` で各ゴールの**turn1 が番号付き計画だけ
を出して PLAN_READY で停止**（status `awaiting`／pill 承認待ち）、計画は status.json(`workers[].plan`)に出る。
ユーザーは**そのまま承認 or 編集を steer で送る**→既存の steer 経路で次ターンに注入され実行開始。worker に
`plan_mode`＋`awaiting`状態。test_planner 14/14（計画抽出・承認待ち・steer再開・実行・未完計画nudge）。

### 差別化: レビューパネルをフリート（並列の主UX）へ
`RefuterSession` に lens を持たせ、worker が**観点ごとに独立レビュアーを逐次（非ブロッキング）実行→多数決集約**。
`fleet_runner --panel` / `code_task --panel`。これで自然言語コーディングでも N 独立レビュアーのパネルが効く。
test_fleet_refute に panel 2 ケース追加（過半REFUTED→差し戻し／少数→done）= 12/12。

### operator A（foundry）状況
`tools/foundry.py`(forge_tool=書込＋compile検証＋tools/auto/へstage, forge_list/read/delete)＋
`auto_loader.load_auto_tools()`(起動時登録)は**実装済・安全**(restart-to-activate、版依存ホット登録は回避)。
残=「agent がタスク中にツールを要求して forge」するループ統合（loose結合・コーディングでの価値限定・Copilotは
再起動後認識のため、別途の集中タスク推奨）。

全体テスト **121/121**（acceptance20・fleet-verify12・folder-verify8・relay-loop9・code-task12・refuter15・
fleet-refute12・trace11・repo-map9・planner14）、`main.py` ビルド成功。

## 限界・残（正直に）

- ゲートはユーザ（オペレータ）がチェックを与えたゴールにだけ効く。チェック無しゴールは
  従来どおり自己申告 DONE を信頼する。folder_coder `--verify` は Python 構文（py_compile）
  までを自動付与；意味的正しさは `--check-cmd` でプロジェクトのテストを指定して初めて担保。
- チェックは別プロセスで走るためフリートは固まらないが、極端に長いビルドは当該ワーカーの
  検証が長引く（タイムアウトで打ち切り）。
- `verify_ops.py`（既存 MCP 検証ツール）とは別系統。ゲートは枠側で `acceptance.py` を直接
  使う（`require_unlocked` ゲートに触れない＝枠はローカルの信頼主体、spec §1）。
