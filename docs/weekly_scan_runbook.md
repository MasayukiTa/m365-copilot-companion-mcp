# 週次走査 実行手順書 (Weekly Scan Runbook)

作成: 2026-11-04 / 対象リポジトリ: `resonac-mcp`

製造データ解析の「取りこぼし」を週に1回走査し、検出があれば改善提案まで行う週次走査の、**人が1コマンドで走らせる方法**と、**OS のスケジューラへ登録する手順**を記す。スケジュール登録は本書では手順の記述のみとし、登録の実行はオーナーが決める。

> 注: `docs/` 直下の `*.md` は `.gitignore`(`/docs/*.md`)で無視されるため、本書はトラッキングできるよう `.gitignore` に許可行(`!/docs/weekly_scan_runbook.md`)を追加してある。

---

## 0. 前提と原則

- 取り逃し防止の原則: **A(効果なし確認)だけを当面の対象外**とし、B・C・D は見込みのある取りこぼし候補として毎週走査対象に残す。E(構造問題)は契機の有無のみ確認。GO でないと判定しても走査対象から外すのは A のみ。
- 判定器・基準・Δmin・補正は**変更しない**。順序の信頼時計(登録順序)の記録は登録済みのものを使い、順序の偽装はしない。
- 出力先は `C:\Users\M118A8586\Documents\製造データ解析_生記録\` 配下(既定)。H ドライブと SharePoint には書かない。
- 台帳(走査対象の台帳)は次の2ファイル:
  - `C:\Users\M118A8586\Documents\製造データ解析_生記録\週次走査_20261104\00_台帳.md`
  - `C:\Users\M118A8586\Documents\製造データ解析_生記録\週次走査_20261104\台帳.json`

### 棚卸しの現況(2026-11-04 時点, active 53解析)

| 分類 | 件数 | 週次での扱い |
|---|---|---|
| GO 採用 | 6 | 追跡のみ |
| B 順序不成立 | 21 | 候補: 順序成立の新ロットで確認 |
| C ロット不足 | 17 | 候補: 規定数到達を DB で計数 |
| D 取得/入力誤り | 3 | 候補: 再取得しやり直し |
| A 効果なし確認 | 1 | 当面の対象外 |
| E 判定不能(構造) | 5 | 契機のみ確認 |

走査対象 (B/C/D) = 41 行。A の 1 行のみ対象外。件数は台帳確定版 `週次走査_20261104/00_台帳.md` の分類サマリに一致する。

---

## 1. エントリポイントと入出力の契約

週次走査の本体はリポジトリの `scripts/weekly_scan/` パッケージ。CLI エントリポイントは `scripts/weekly_scan/cli.py`。本書はこのエントリポイントを前提とする(本体スクリプトと共通基盤 `scripts/weekly_scan_foundation` は別 PR で追加される)。モジュール実行のため、リポジトリ直下を作業ディレクトリにする。

- **エントリポイント**: `python -m scripts.weekly_scan.cli`
- **主な引数**(試運転記録 `週次走査_20261104/試運転.md` のコマンド相当に一致):
  - `--ledger <path>`: 台帳 JSON。例 `C:/Users/M118A8586/Documents/製造データ解析_生記録/週次走査_20261104/台帳.json`。走査後、各行の最終走査日・次の契機・scan_notes が更新されて同じファイルに書き戻される(ラウンドトリップ)。
  - `--docs-root <dir>`: 解析ディレクトリ・報告の出力先の基点。例 `C:/Users/M118A8586/Documents`
  - `--dry-run`: DB 計数・判定器を no-op プロバイダに差し替え、台帳のラウンドトリップ・走査日更新・次の契機更新・scan_notes 追記というオーケストレーションのみを検証する(試運転・疎通確認用)。DB も判定器も呼ばないため judged=0 / GO=0 が正常。
- **終了コード**:
  - `0`: 正常終了(dry-run を含む)。検出なしでも 0。
  - `2`: 共通基盤 `scripts/weekly_scan_foundation` が未提供。stderr に `shared foundation not available` を出して終了(クラッシュしない)。
- **サマリ出力**(標準出力 1 行)。例(dry-run):
  `weekly-scan (a)+(b) 2026-11-04: B/C rows=38 judged=0 GO=0; D rows=3 re-judged=0 GO=0; written=yes.`
- **出力(1回の走査につき)**:
  - 更新後の台帳 `台帳.json`(入力ファイルをそのまま更新。最終走査日・次の契機・scan_notes を反映)
  - 検出時のみ 報告 `報告_<解析ID>.pptx` と `報告_<解析ID>.md`、および改善提案と追跡項目の台帳追記

---

## 2. 人が1コマンドで走らせる方法

### 2.1 Windows (PowerShell / コマンドプロンプト)

リポジトリ直下で、同梱の venv を使って実行する。

```bat
cd /d C:\Users\M118A8586\resonac-mcp
.venv\Scripts\python.exe -m scripts.weekly_scan.cli ^
  --ledger "C:/Users/M118A8586/Documents/製造データ解析_生記録/週次走査_20261104/台帳.json" ^
  --docs-root "C:/Users/M118A8586/Documents"
```

試運転(DB 計数・判定器を no-op に差し替え、台帳ラウンドトリップのみ検証。初回や環境変更後の疎通確認に使う):

```bat
cd /d C:\Users\M118A8586\resonac-mcp
.venv\Scripts\python.exe -m scripts.weekly_scan.cli ^
  --ledger "C:/Users/M118A8586/Documents/製造データ解析_生記録/週次走査_20261104/台帳.json" ^
  --docs-root "C:/Users/M118A8586/Documents" ^
  --dry-run
```

### 2.2 Linux / cron 相当(CI・検証環境)

```sh
cd ~/resonac-mcp
.venv/bin/python -m scripts.weekly_scan.cli --ledger "$LEDGER" --docs-root "$DOCSROOT"
```

---

## 3. スケジューラへ登録する手順(登録はオーナーが決める。ここでは記述のみ)

> 以下は手順の記述であり、本書の作業では登録を実行しない。

### 3.1 Windows タスクスケジューラ

毎週月曜 08:00 に走らせる例。管理者 PowerShell で:

```powershell
$action  = New-ScheduledTaskAction `
  -Execute 'C:\Users\M118A8586\resonac-mcp\.venv\Scripts\python.exe' `
  -Argument '-m scripts.weekly_scan.cli --ledger "C:/Users/M118A8586/Documents/製造データ解析_生記録/週次走査_20261104/台帳.json" --docs-root "C:/Users/M118A8586/Documents"' `
  -WorkingDirectory 'C:\Users\M118A8586\resonac-mcp'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 08:00
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
Register-ScheduledTask -TaskName 'ResonacWeeklyScan' `
  -Action $action -Trigger $trigger -Principal $principal `
  -Description '製造データ解析 週次走査'
```

GUI で登録する場合: タスクスケジューラ → 基本タスクの作成 → トリガー「毎週・月曜 08:00」→ 操作「プログラムの開始」→ プログラム `C:\Users\M118A8586\resonac-mcp\.venv\Scripts\python.exe`、引数 `-m scripts.weekly_scan.cli --ledger "...台帳.json" --docs-root "C:/Users/M118A8586/Documents"`、開始場所 `C:\Users\M118A8586\resonac-mcp`。

登録の確認・削除:

```powershell
Get-ScheduledTask -TaskName 'ResonacWeeklyScan'
# 削除する場合
Unregister-ScheduledTask -TaskName 'ResonacWeeklyScan' -Confirm:$false
```

### 3.2 cron 相当(Linux)

毎週月曜 08:00。`crontab -e` に追記:

```cron
0 8 * * 1 cd $HOME/resonac-mcp && .venv/bin/python -m scripts.weekly_scan.cli --ledger "$HOME/Documents/製造データ解析_生記録/週次走査_20261104/台帳.json" --docs-root "$HOME/Documents" >> $HOME/resonac-mcp/logs/weekly_scan.log 2>&1
```

---

## 4. 走査後の確認項目

1. 標準出力のサマリ行(`rows` / `judged` / `GO` / `written`)を確認する。
2. 台帳(`台帳.json`)の走査対象(B/C/D)の全行で「最終走査(last_scanned)」が当日に更新されたか。B・C の「次の契機」(規定数到達予定日)が最新化されたか。A の行は更新されないのが正常。
3. 検出(順序成立の採用、または強い候補)があれば `報告_<解析ID>.pptx` と `報告_<解析ID>.md` が生成され、改善提案と追跡項目が台帳に追記されたか。
4. 取りこぼし無しの基準: 走査対象(B/C/D)の行数と last_scanned 更新行数が一致すること。2026-11-04 の試運転では B/C/D = 41 行すべてが更新され、取りこぼし 0 を確認している(`週次走査_20261104/試運転.md`)。

---

## 5. トラブル時

- 終了コード 2 / `shared foundation not available` → 共通基盤 `scripts/weekly_scan_foundation` が未マージ。該当 PR のマージを待つ。疎通だけ見たいときは `--dry-run` を使う。
- `ModuleNotFoundError: scripts.weekly_scan` → 作業ディレクトリがリポジトリ直下でない。`cd C:/Users/M118A8586/resonac-mcp` してから実行する。
- DB 接続不可 → 既存解析と同じ ODBC 設定・権限を確認し再実行。`--dry-run` は DB を呼ばないので切り分けに使える。
- 台帳が更新されない → `--ledger` のパスと書き込み権限を確認する。サマリ行の `written` を見る。
- 途中失敗 → 台帳の「最終走査」が当日未更新の解析は未走査。原因解消後に再実行すれば未走査分から再開する。

---

## 6. 試験(登録前・変更後に実行)

単体試験は `scripts/weekly_scan/tests/` にある。リポジトリ直下で実行する。

```bat
cd /d C:\Users\M118A8586\resonac-mcp
.venv\Scripts\python.exe -m pytest scripts/weekly_scan/tests/ -q
```

あわせて次を確認する。

- 未定義名ゲート: `ruff check --select F821 scripts/weekly_scan`
- CI マニフェスト監査: `python scripts/check_ci_test_manifest.py`

新しいテストファイルを足したら、必ず `.github/workflows/ci.yml` の明示リストに追加する(追加しないと CI で収集されず、緑に見えて一度も走らない)。
