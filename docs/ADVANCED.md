# 上級者向け機能

relay / fleet の細かい制御、bridge UI、ODBC 接続、ツール自己生成、性能ベンチの詳細など、本文に載せない話をまとめます。全体像は [README](../README.md)、内部構造は [ARCHITECTURE.md](ARCHITECTURE.md) を先に読んでください。

---

## ツールカタログ（全 138 ツールの分類）

`main.py` の `TOOLS` タプルに登録された関数群が、すべて MCP ツールとして公開されます。実行中は `list_my_tools` で**いまの環境で有効な**カタログを引けます。

**前提タグの凡例**: 🟢 追加要件なし（クローン後すぐ動く） / 🪟 Windows 専用 / 📦 追加インストールが必要 / ☁ 実行すると外部サービスにデータが出る

| カテゴリ | 主なツール | 前提 | 何のために |
|---|---|---|---|
| **コード実行** | `run_python`, `shell_exec`, `run_python_in_background`, `run_in_background`, `job_wait`, `job_status`, `job_output`, `job_list`, `job_kill` | 🟢 | コードを走らせる、長いやつは投げて待つ、暴走したら殺す |
| **PowerShell** | `pwsh_exec`, `pwsh_exec_file`, `shell_which` | 🪟 | PowerShell 5.1 / 7 を直接叩く（`-NoProfile -NonInteractive -ExecutionPolicy Bypass` 込み） |
| **プロセス / サービス / レジストリ** | `process_list`, `process_info`, `process_kill`, `service_status`, `registry_read` | 🪟📦`psutil` | タスクマネージャ + サービス + レジストリの読み口（kill は unlock 必須） |
| **ファイル I/O** | `read_file`, `write_file`, `append_file`, `list_directory`, `glob`, `find_files`, `copy_path`, `move_path`, `trash_path`, `create_directory`, `delete_path` | 🟢 | 許可ディレクトリ内のファイルを自在に。`trash_path` はゴミ箱送りで復元可 |
| **ファイル/ディスク調査** | `hash_file`, `find_duplicates`, `dir_size`, `file_metadata` | 🟢 | ハッシュ・重複検出・容量・メタ情報 |
| **編集・検索** | `grep`, `replace_in_file`, `multi_edit`, `diff_files`, `python_check` | 🟢 | 原子的な複数編集 |
| **Git** | `git_status`, `git_diff`, `git_log`, `git_branch`, `git_blame`, `git_add`, `git_commit`, `git_checkout` | 📦`git` | 読みも書きも |
| **表 / JSON** | `read_excel`, `write_excel`, `summarize_table`, `read_json`, `write_json` | 🟢 | Excel / CSV / JSON を一級市民として扱う |
| **PDF** | `read_pdf`, `pdf_info` | 🟢 | デジタル PDF のテキスト抽出 |
| **OCR** | `ocr_image`, `ocr_pdf` | 📦`Tesseract`（+`Poppler`） | スキャン画像/PDF の文字起こし |
| **画像（自己検証）** | `read_image`, `image_info` | 🟢 | エージェントが自分で作った図を見返して確認 |
| **PowerPoint** | `create_pptx`, `pptx_from_markdown`, `pptx_info`, `pptx_add_slide`, `pptx_add_image`, `pptx_add_table`, `pptx_replace_image` | 🟢 | スライド生成・画像/表埋込 |
| └ PNG 自己確認 | `pptx_export_png` | 🪟📦`PowerPoint 本体` | 各スライドを PNG 化して目視 |
| **Word (.docx)** | `create_docx`, `docx_from_markdown`, `docx_info`, `read_docx` | 🟢 | 文書生成と読解 |
| **Outlook**（ローカル退避路） | `outlook_inbox`, `outlook_send_mail`, `outlook_calendar`, `outlook_create_event` | 🪟📦`Outlook 本体` | Graph コネクタが使えない時用。COM で今ログイン中の Outlook を借りる。送信は既定で下書き保存 |
| **クリップボード / スクリーン** | `clipboard_get`, `clipboard_set`, `screenshot` | 🪟 | 「いまコピーしたこれ見て」「いま画面に映ってるもの撮って」 |
| **図 / 数式** | `render_diagram`☁, `render_mermaid_png`☁, `render_math`🟢 | ☁ Kroki / `render_math` は外部不要 | アーキ図と数式を生成。社外秘は render_diagram に出さない |
| **Web** | `web_fetch`, `web_search`, `web_search_news`, `github_file` | ☁ | DuckDuckGo 検索・URL 取得。Copilot 標準検索が使えれば不要 |
| **DB (SQLite)** | `sqlite_tables`, `sqlite_schema`, `sqlite_query`, `sqlite_to_excel` | 🟢 | ローカル `.sqlite` を read-only で |
| **DB (ODBC)** | `odbc_drivers`, `odbc_connections`, `odbc_tables`, `odbc_columns`, `odbc_query`, `odbc_to_excel` | 📦`ODBC ドライバ`+接続設定 | 社内 SQL Server / Azure SQL。Windows/Entra 認証継承、read-only 強制 |
| **永続記憶** | `memory_save`, `memory_load`, `memory_list`, `memory_delete` | 🟢 | セッション横断のメモ |
| **スケジュール** | `schedule_create`, `schedule_list`, `schedule_info`, `schedule_run_now`, `schedule_delete` | 🪟 | 「毎週金曜 9 時に週報生成」 |
| **ファイル監視** | `watcher_start`, `watcher_events`, `watcher_stop` | 📦`watchdog` | フォルダ変更検知 |
| **アーカイブ** | `zip_list`, `zip_extract`, `zip_create` | 🟢 | zip-slip 対策付き |
| **通知** | `notify_desktop` | 🪟 | `job_wait` と組ませて能動通知 |
| **環境管理** | `env_info`, `pip_install`, `which`, `list_my_tools` | 🟢 | 実行環境の自己診断、その場 install |
| **セキュリティ** | `unlock`, `list_unlocked` | 🟢 | 変更系ツールの IP 単位パスワード解錠 |
| **その他** | `todo_write`, `todo_list`, `todo_clear` | 🟢 | エージェント自身の計画用スクラッチパッド |

---

## ツールを増やす — エージェント自身も増やせる

手動で増やす場合: `tools/*.py` に Python 関数を 1 つ書き、`main.py` の `TOOLS` タプルに追加して再起動するだけ。docstring が LLM の説明文になります。

**エージェント自身が、その場で新しいツールを書けます**:

1. `pip_install` で必要なライブラリを入れる
2. `write_file` で `tools/your_ops.py` を生成する
3. `run_python` でロジックを動作確認する

ここまでをチャットの中で完結し、あとは `main.py` の `TOOLS` に 1 行足して再起動すれば、自分で書いたツールが常設化します。「◯◯の API を叩くツールが欲しい」と頼めば、エージェントが雛形を書いて検証し、登録手順まで提示します。

コーディングループの中では `FORGE: <名前>` ＋ Python ブロックで再利用ツールを自作し、構文検証して `tools/auto/` に常設化できます（`--forge`）。

---

## 自己検証ループ（Trust but Verify）

エージェント事故の大半は「完了したと言ったけど実は何もしていない」「画像が入っていない pptx を作って完了報告」。本サーバーは自分の出力を**見返す**ための 2 ツールを持ちます:

- `read_image(path)`: PNG/JPG を base64 data URI として返す。Vision 対応モデル（M365 Copilot 内の Opus 含む）はそのまま読める
- `pptx_export_png(pptx_path)`: PowerPoint を COM 経由で各スライド PNG にエクスポート（🪟 PowerPoint 本体が必要）

典型ループ:

```
run_python       → chart.png を保存
read_image       → エージェントが目視確認、ズレてたら直す
create_pptx      → chart.png を report.pptx に埋め込む
pptx_export_png  → 各スライドの PNG をエージェントが流し見
notify_desktop   → 「report.pptx 完成」
```

system prompt にこの順序を 1 行入れておけば「画像なし pptx を完成と報告」事故は消えます。

---

## 自動起動（ログオン時にスタック全体を起動）

`scripts\register-supervisor.ps1` を 1 回実行すると、次回ログオンから `start_all_hidden.vbs`（＝サーバー・トンネル・bridge・全 UI）が管理者権限不要で自動起動します。仕組みは per-user Startup フォルダのショートカット（Task Scheduler が組織ポリシーで弾かれる環境でも通る）。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register-supervisor.ps1   # 登録
powershell -ExecutionPolicy Bypass -File scripts\unregister-supervisor.ps1 # 解除
```

詳細（動作原理・確認方法・自己修復動作）は [scripts/AUTOSTART.md](../scripts/AUTOSTART.md)。

---

## relay を直接叩く（一回だけのセットアップ）

再ログイン不要・Playwright のブラウザ DL も不要（既にログイン済みの Edge に attach するだけ）:

```powershell
.\.venv\Scripts\pip.exe install -r requirements-relay.txt

# Edge を debug ポート付きで起動（Chrome でも可）
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222
# → その Edge で M365 Copilot を開き、MCP エージェントで新規チャットを開始し、会話 URL をコピー
# 【推奨】普段使いの Edge とは別の専用・隔離 Edge を使う: .\scripts\start_companion_edge.ps1

.\.venv\Scripts\python.exe relay\copilot_autopilot_relay.py `
  --conversation-url "https://m365.cloud.microsoft/chat/agent/.../conversation/..." `
  --goal "data.csv を作り、合計と平均を出す stats.py を書き、self-test を PASS させる" `
  --max-turns 12
```

---

## 自律コーディング・エージェント（Claude Code 相当）

relay の上に Claude Code のような自律コーディング体験を載せています。`relay/code_task.py` に自然言語 1 行を投げるだけ:

```powershell
# よく使う形
-m relay.code_task -i "落ちてるテストを直して" -f C:\proj              # 自然言語＋自動検証
-m relay.code_task -i "リファクタして" -f C:\proj --plan               # 計画提示→承認→実行
-m relay.code_task -i "堅牢化して"   -f C:\proj --panel                # 3観点レビューパネル
-m relay.fleet_runner --goals-file goals.txt --max-concurrent 3        # N 本並列
```

頭脳は M365 Copilot の中身＝Opus 4.8（Claude 本体と同じ）。機構としては Claude Code の ~80% パリティ。主な機能:

- **検証ゲート**（`relay/acceptance.py`）— Copilot の「DONE」を鵜呑みにせず、枠がローカルでテスト/コンパイルを実行し、通らなければ実際の失敗出力を突き返す。通るまで完了にしない。
- **検証の自動判定**（`relay/project_introspect.py`）— pytest があれば pytest、無ければ compile、Node なら npm test を自動採用。
- **リポジトリ地図**（`relay/repo_map.py`）— AST 解析したツリー＋シグネチャ＋docstring を注入し、盲目 grep でなく地図を持って着手。
- **プラン提示→承認→実行**（`relay/planner.py`）— `--plan` で番号付き計画を出して一時停止。承認 or steer で実行開始。
- **多視点レビューパネル**（operator B）— 正しさ/境界値/セキュリティの独立レビュアーで多数決。`--refuter`（単一）/`--panel`（3観点）。
- **全ターン監査ログ＋kill-switch、ツール使用トレース**（`MCP_TRACE_TOOLCALLS=1` で編集/実行内容を JSONL 記録）。

制御ロジックはブラウザ無しで決定的にテスト（138 本超のユニットテスト）し、実機 Copilot 相手の end-to-end もライブ実証済み。

---

## fleet 並列と RAM 連動 autoscale

`relay/fleet_runner.py` は複数の M365 Copilot 会話を同時に駆動します。各ゴールが自分の会話を持ち、1 スレッドのノンブロッキング・ラウンドロビンで前進、終了した瞬間にタブを閉じて RAM を解放し、次のゴールを空きスロットに開きます。

```powershell
# goals をインラインで
.\.venv\Scripts\python.exe -m relay.fleet_runner --agent-url <URL> -g "goal A" -g "goal B"
# 1 行 1 ゴール / 1 行 1 JSON（JSON 行は acceptance checks を持てる）
.\.venv\Scripts\python.exe -m relay.fleet_runner --agent-url <URL> --goals-file goals.txt
```

`checks` を持つゴールは単一 relay と同じ acceptance gate を通ります。`--refuter` / `--panel` で独立レビュアー / 3観点パネルを追加。

**RAM 連動 autoscale**: M365 Copilot タブは重い SPA（各 ~0.3–0.6 GB）なので、多数同時に開くと RAM が枯渇します。`--autoscale`（またはコックピットのトグル）で、毎ループ空き物理メモリから並列数上限を再計算します（`relay/relay_fleet.py` の `ram_target_cap` / `auto_concurrency`）。RAM に余裕がある間は 1 ループにつき最大 1 タブずつ増やし、逼迫したら緩やかに減らします（走行中のワーカーは殺さず、新規を開くのを止めるだけ）。`--autoscale-headroom-mb` / `--autoscale-per-tab-mb` / `--autoscale-max` で境界を調整。autoscale 無しなら `--max-concurrent` が固定上限（`0` = 起動時の空き RAM から自動）。

**watchdog・復旧・再開**: 別の watchdog スレッドが `status.json` を tail し、`--stall-s` を超えて進まなければ専用 Edge を wedge とみなしハードリセット。run ループは死んだ CDP コンテキスト（`FleetContextLost`）を検知して新しい Edge に再接続し、未完のゴールを（acceptance checks 込みで）再開します。ただし acceptance eval 中（`verifying` 状態や未来の `eval_busy_until`）はリセットしないので、長い正当な検証（Docker テスト等）を wedge と誤認しません。起動前の auto-recycle（肥大/低 RAM なら run 前にハードリセット）もあります（`relay/edge_recover.py`）。

---

## FleetCockpit（並列実行の操作盤）

`ui/FleetCockpit.cs` は JS フリーの WPF アプリ（Windows 同梱の `csc.exe` でビルド）で、`.fleet/status.json` を tail し `.fleet/commands.json` を書いて動作中の fleet を制御します。`ui\build_cockpit.bat` でビルド＆起動。できること:

- 各ワーカーの状態 / ターン / 検証状態 / 最新応答をライブ監視
- ワーカーを停止・解放（タブを解放）
- 最大同時タブ数の変更・RAM 連動 autoscale のトグル（上限付き）
- 動作中の fleet に新ゴールを追加
- ワーカーを steer（次ターンになるリダイレクトを注入）
- 停止したゴールを retry（単発、または上限付き auto-retry）
- ワーカーの会話を名前でメインチャットに開く

ゴール欄はコーディング用スラッシュコマンド（`/code`, `/fix`, `/test`, `/refactor`, `/doc`, `/review`, `/research`）を、メインチャットはプロンプトテンプレート（`/help`, `/summarize`, `/translate`, `/plan`, `/critique`, `/proofread`, `/rewrite`, `/brainstorm`, `/steps`, `/eli5`, `/proscons`, `/table`）＋ `/research` `/analyze` を出します。

各 fleet 会話は `.fleet/conversations.json` に登録され、各ワーカーの全ターン transcript が `.fleet/transcripts/` に書かれます。会話を開くと、チャット UI はライブ DOM スクレイピングより on-disk transcript を優先するので、動作中の fleet タスクを active な companion Edge を乱さず閲覧できます。

---

## bridge 用の専用 Edge（bridge と fleet を同時に動かす）

```powershell
# bridge は別プロファイル (copilot-bridge-edge) + 別ポート (:9223) で起動
.\scripts\start_bridge.ps1
.\scripts\start_bridge.ps1 -SignIn       # 初回サインインが必要なら
.\scripts\start_bridge.ps1 -Keepalive    # 常時稼働（クラッシュ時も自動再起動）
```

`start_bridge.ps1` は bridge 専用 Edge（`:9223`）を立て、`bridge/copilot_bridge.py` を起動します。`http://127.0.0.1:8765` でネイティブチャット UI が開きます。fleet の Edge（`:9222`）とは完全に別プロファイルなので、fleet 走行中でも同時に使えます。

---

## 専用 companion Edge のウィンドウモード

`scripts/start_companion_edge.ps1` は隔離 companion Edge を起動します。ウィンドウモード:

- `-Foreground` — 可視ウィンドウ。**安定した既定**、初回サインインとトラブル対応向け。
- `-Headless` — `--headless=new`: ウィンドウ無し（タスクバーにも出ない）が CDP・SSO・送信は全て動く真のバックグラウンド。
- `-Background` — 最小化 / 別仮想デスクトップに退避（`edge_keeper.ps1` / `move_companion_to_desktop.ps1` が管理）。実験的（backgrounded CDP Edge の駆動は不安定なので foreground/headless 推奨）。
- `-Surface` — 動作中の companion Edge を前面に戻す（対話サインインが要る時）。
- `-HardReset` — companion Edge を kill しセッション復元状態を wipe してから再起動（wedge したタブを復元しない）。watchdog が呼ぶ復旧経路。

---

## ODBC 接続（社内 DB）

`.env` に `MCP_DB_<NAME>=` を追記すると `odbc_query` の名前付き接続になります。認証は Windows / Entra ID（`Trusted_Connection=Yes` または `Authentication=ActiveDirectoryIntegrated`）なのでパスワードは保存しません。エージェントはログイン中ユーザーの権限の範囲でしか SQL を投げられません。read-only 強制で `SELECT / WITH / EXEC / SHOW / DESCRIBE` のみ許可。

```
# Windows 認証（オンプレ SQL Server）
MCP_DB_SAMPLE=Driver={ODBC Driver 18 for SQL Server};Server=YOUR-SQL-HOST;Database=YOUR_DB;Trusted_Connection=Yes;TrustServerCertificate=Yes
# Entra ID 統合（Azure SQL）
MCP_DB_SAMPLE_AZURE=Driver={ODBC Driver 18 for SQL Server};Server=tcp:YOUR-SERVER.database.windows.net,1433;Database=YOUR_DB;Authentication=ActiveDirectoryIntegrated;Encrypt=Yes
```

---

## Claude Desktop / Claude Code に繋ぐ（M365 なし）

Dev Tunnel は不要です。`.\scripts\start.ps1` でサーバーを起動したら、次を `~/.claude/claude_desktop_config.json`（Claude Desktop）または `.claude/settings.json`（Claude Code）に追記:

```json
{
  "mcpServers": {
    "companion": {
      "transport": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <MCP_API_KEY の値>" }
    }
  }
}
```

この経路は devtunnel も Entra ID も Copilot ライセンスも不要で、Claude Desktop さえあれば誰でも使えます。

---

## 性能ベンチの詳細

要約は [README の性能セクション](../README.md#性能)。ここは再現手順と正直な注記です。

### HumanEval 164 — 100%（first-pass 98.2%）

採点は `bench/score.py` が各 `solution.py` に隠しテスト（canonical test）を再実行する ground-truth。頭脳は M365 Copilot 内の Opus 4.8 で、複数ターンで自分のコードを `run_python` 実行・反復し、受入チェックを通し、一時的失敗は指数バックオフでリトライするスキャフォールド込みの数値。Anthropic 公表の Opus 系 HumanEval（~90–92%）は素のモデルの single-shot なので直接比較不可。「頭脳が上」ではなく「スキャフォールドが効いている」。

再現: `python -m bench.build --stride 1 --limit 164` → fleet で実行 → `python -m bench.score`。

### SWE-bench Lite 300 — 71.7%（215/300）

実在 OSS のバグを隠しテストが通るまで直すタスク。grader 非リーク（`checks=N`・採点はオフライン）・WSL2 Docker 上の公式採点でフル完走。Wilson 95% CI [66.3%, 76.5%]・EVALERR 0。汎化確認として SWE-bench Verified の非 burned 200 件でも 153/200 = 76.5%（Wilson 95% CI [70.2%, 81.8%]）。

**60 件での失敗分析**（scaffold 強化の元）: ベースライン 40/59 = 67.8% → 強化 47/60 = 78.3%。r1 の失敗を故障クラス（検証ループ未閉鎖／多点修正の片肺／層違い／抑制vs表出）に類型化し、ベンチに過適合しないドメイン一般な修正だけを投入。デバッグに使った問題は burned 扱いでスコア主張から除外。**不偏の代表値はフル 300 の 71.7%**（60 件スライスが高いのは難易度差と小 N の揺れ）。

再現/詳細: `python bench/swe_lite300_scorecard.py`・`bench/SCORECARD_swebench_lite300_strong.md`。SWE-bench 公式ハーネスを acceptance gate に組み込む検証ストレステストは `bench/swe_check.py`。

### GAIA — 一般アシスタント能力（公式採点）

Meta/HF の一般 AI アシスタント・ベンチ。解いたのは M365 Copilot エージェント本体（Web グラウンディング有の既定 Copilot）で Anthropic API ではありません。採点は GAIA 公式スコアラ（正規化＋完全一致）。165 問中ファイル添付必須の 38 問は :8011 エンドポイントが受け取れず除外 → text-only が対象。custom Copilot Studio エージェントは設計上一般質問を断るため、素の既定 Copilot（`/chat/`・Web 有）に向けて測ります。GAIA validation はトップ級エージェントでも text-only で概ね 40–70% 帯。

再現: 既定 Copilot に向けた `:8011` を立て、`python bench/gaia/runner.py`（公式スコアラ `bench/gaia/scorer.py`）。エラー回収は `python bench/gaia/retry_controller.py`。

---

← [README に戻る](../README.md)
