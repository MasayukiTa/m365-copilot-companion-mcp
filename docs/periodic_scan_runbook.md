# 定期走査 実行手順書 (Periodic Scan Runbook)

作成: 2026-11-04 / 対象リポジトリ: `m365-copilot-companion-mcp`

データ解析の「取りこぼし」を定期的に走査し、検出があれば改善提案まで行う定期走査の、**人が1コマンドで走らせる方法**と、**OS のスケジューラへ登録する手順**を記す。スケジュール登録は本書では手順の記述のみとし、登録の実行はオーナーが決める。

> 注: `docs/` 直下の `*.md` は `.gitignore`(`/docs/*.md`)で無視されるため、本書はトラッキングできるよう `.gitignore` に許可行(`!/docs/periodic_scan_runbook.md`)を追加してある。

---

## 0. 前提と原則

- 取り逃し防止の原則: **A(効果なし確認)だけを当面の対象外**とし、B・C・D は見込みのある取りこぼし候補として毎回の走査対象に残す。E(構造問題)は契機の有無のみ確認。GO でないと判定しても走査対象から外すのは A のみ。
- 判定器・基準・\u0394min・補正は**変更しない**。順序の信頼時計(登録順序)の記録は登録済みのものを使い、順序の偽装はしない。
- 出力先・台帳・解析ディレクトリの実パスは環境ごとに異なる。本書では `<user>` と `<...>` で伏せる。サイト固有の値は実行時に引数で与える(リポジトリには埋め込まない)。
- 台帳(走査対象の台帳)は JSON ファイル 1 つ(例: `台帳.json`)。走査後、同じファイルに最終走査日・次の契機・scan_notes が書き戻される(ラウンドトリップ)。

### 分類ごとの扱い

| 分類 | 定期走査での扱い |
|---|---|
| GO 採用 | 追跡のみ |
| B 順序不成立 | 候補: 順序成立の新ロットで確認 |
| C ロット不足 | 候補: 規定数到達を DB で計数 |
| D 取得/入力誤り | 候補: 再取得しやり直し |
| A 効果なし確認 | 当面の対象外 |
| E 判定不能(構造) | 契機のみ確認 |

走査対象は B/C/D。A のみ対象外。

---

## 1. エントリポイントと入出力の契約

定期走査の本体はリポジトリの `scripts/periodic_scan/` パッケージ。処理は 2 つの実在エントリに分かれる。モジュール実行のため、リポジトリ直下を作業ディレクトリにする。サイト固有の接続・判定器は **wiring モジュール**として外部から注入し、リポジトリ本体には含めない。

### 1.1 B/C 確認 + D 再取得ドライバ

- **エントリポイント**: `python -m scripts.periodic_scan.run_bc_d`
- **主な引数**:
  - 位置引数 `<ledger>`: 台帳 JSON のパス。走査後、各行の最終走査日・次の契機・scan_notes が更新されて同じファイルに書き戻される。
  - `--wiring <module>`(必須): 実行環境のポート(request_loader / lot_counter / data_fetcher / judge)を提供するモジュール名。実 DB(ODBC)と凍結判定器の結線はここに置く。リポジトリにはサイト固有の結線を置かないため必須。
  - `--dry-run`: DB 計数・判定器を no-op プロバイダに差し替え、台帳のラウンドトリップ・走査日更新・次の契機更新・scan_notes 追記というオーケストレーションのみを検証する。DB も判定器も呼ばないため judged=0 / GO=0 が正常。
  - `--run-log <path>`: 実行ログの出力先(任意)。
- **終了コード**: `0` 正常終了(dry-run・検出なしを含む)。wiring モジュールが読み込めない等は非 0。

### 1.2 スクリーニング(c) + 報告(d)

- **エントリポイント**: `python -m scripts.periodic_scan.run_screening_report`
- **主な引数**(すべて必須):
  - `--items <path>`: 全結果項目と記録数の JSON 配列。
  - `--inputs-dir <dir>`: `<result_item>.json` を置いたディレクトリ。
  - `--prior <path>`: 過去のスクリーニング記録数 `{item:count}` の JSON(任意)。
  - `--prereg <path>`: 事前登録 JSONL の出力先。
  - `--report-dir <dir>`: md/pptx 報告の出力ディレクトリ。
  - `--ledger <path>`: 追跡を追記する台帳 JSON。
- **出力**: 検出時に 報告 `*.md` と `*.pptx`(`python-pptx` 不在時は pptx を飛ばす)、事前登録 JSONL、台帳への追跡追記。

---

## 2. 人が1コマンドで走らせる方法

### 2.1 Windows (PowerShell / コマンドプロンプト)

リポジトリ直下で、同梱の venv を使って実行する。

```bat
cd /d C:\\Users\\<user>\\m365-copilot-companion-mcp
.venv\\Scripts\\python.exe -m scripts.periodic_scan.run_bc_d ^
  "<...>/台帳.json" ^
  --wiring <your_wiring_module> ^
  --run-log "<...>/run.log"
```

試運転(DB 計数・判定器を no-op に差し替え、台帳ラウンドトリップのみ検証。初回や環境変更後の疎通確認に使う):

```bat
cd /d C:\\Users\\<user>\\m365-copilot-companion-mcp
.venv\\Scripts\\python.exe -m scripts.periodic_scan.run_bc_d "<...>/台帳.json" --wiring <your_wiring_module> --dry-run
```

検出があった項目の スクリーニング + 報告:

```bat
.venv\\Scripts\\python.exe -m scripts.periodic_scan.run_screening_report ^
  --items "<...>/items.json" ^
  --inputs-dir "<...>/inputs" ^
  --prereg "<...>/prereg.jsonl" ^
  --report-dir "<...>/reports" ^
  --ledger "<...>/台帳.json"
```

### 2.2 Linux / cron 相当(CI・検証環境)

```sh
cd ~/m365-copilot-companion-mcp
.venv/bin/python -m scripts.periodic_scan.run_bc_d "$LEDGER" --wiring "$WIRING"
```

---

## 3. スケジューラへ登録する手順(登録はオーナーが決める。ここでは記述のみ)

> 以下は手順の記述であり、本書の作業では登録を実行しない。

### 3.1 Windows タスクスケジューラ

定期的(例: 毎週月曜 08:00)に走らせる例。管理者 PowerShell で:

```powershell
$action  = New-ScheduledTaskAction `
  -Execute 'C:\\Users\\<user>\\m365-copilot-companion-mcp\\.venv\\Scripts\\python.exe' `
  -Argument '-m scripts.periodic_scan.run_bc_d "<...>/台帳.json" --wiring <your_wiring_module>' `
  -WorkingDirectory 'C:\\Users\\<user>\\m365-copilot-companion-mcp'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 08:00
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
Register-ScheduledTask -TaskName 'M365PeriodicScan' `
  -Action $action -Trigger $trigger -Principal $principal `
  -Description 'データ解析 定期走査'
```

登録の確認・削除:

```powershell
Get-ScheduledTask -TaskName 'M365PeriodicScan'
# 削除する場合
Unregister-ScheduledTask -TaskName 'M365PeriodicScan' -Confirm:$false
```

### 3.2 cron 相当(Linux)

毎週月曜 08:00。`crontab -e` に追記:

```cron
0 8 * * 1 cd $HOME/m365-copilot-companion-mcp && .venv/bin/python -m scripts.periodic_scan.run_bc_d "$LEDGER" --wiring "$WIRING" >> $HOME/m365-copilot-companion-mcp/logs/periodic_scan.log 2>&1
```

---

## 4. 走査後の確認項目

1. 実行ログ/標準出力のサマリ(`rows` / `judged` / `GO` / `written`)を確認する。
2. 台帳(`台帳.json`)の走査対象(B/C/D)の全行で「最終走査(last_scanned)」が当日に更新されたか。B・C の「次の契機」(規定数到達予定日)が最新化されたか。A の行は更新されないのが正常。
3. 検出があれば報告 `*.pptx` と `*.md` が生成され、改善提案と追跡項目が台帳に追記されたか。
4. 取りこぼし無しの基準: 走査対象(B/C/D)の行数と last_scanned 更新行数が一致すること。

---

## 5. トラブル時

- wiring が読めない / `shared foundation not available` → `--wiring` に渡すモジュール名と、実環境の結線(`periodic_scan_foundation` 等)の提供状況を確認する。疎通だけ見たいときは `--dry-run` を使う。
- `ModuleNotFoundError: scripts.periodic_scan` → 作業ディレクトリがリポジトリ直下でない。`cd` でリポジトリ直下へ移動してから実行する。
- DB 接続不可 → 既存解析と同じ ODBC 設定・権限を確認し再実行。`--dry-run` は DB を呼ばないので切り分けに使える。
- 台帳が更新されない → 台帳パスと書き込み権限を確認する。サマリの `written` を見る。
- 途中失敗 → 台帳の「最終走査」が当日未更新の解析は未走査。原因解消後に再実行すれば未走査分から再開する。

---

## 6. 試験(登録前・変更後に実行)

単体試験は `scripts/periodic_scan/tests/` にある。リポジトリ直下で実行する。

```bat
cd /d C:\\Users\\<user>\\m365-copilot-companion-mcp
.venv\\Scripts\\python.exe -m pytest scripts/periodic_scan/tests/ -q
```

あわせて次を確認する。

- 未定義名ゲート: `ruff check --select F821 scripts/periodic_scan`
- CI マニフェスト監査: `python scripts/check_ci_test_manifest.py`

新しいテストファイルを足したら、必ず `.github/workflows/ci.yml` の明示リストに追加する(追加しないと CI で収集されず、緑に見えて一度も走らない)。
