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
| B 順序不成立 | 20 | 候補: 順序成立の新ロットで確認 |
| C ロット不足 | 20 | 候補: 規定数到達を DB で計数 |
| D 取得/入力誤り | 3 | 候補: 再取得しやり直し |
| A 効果なし確認 | 1 | 当面の対象外 |
| E 判定不能(構造) | 3 | 契機のみ確認 |

---

## 1. エントリポイントと入出力の契約

週次走査の本体はリポジトリの `scripts/weekly_scan.py`。本書はこのエントリポイントを前提とする(本体スクリプトは別 PR で追加される)。

- **エントリポイント**: `python scripts/weekly_scan.py`
- **主な引数**:
  - `--ledger <path>`: 台帳 JSON。既定 `C:\Users\M118A8586\Documents\製造データ解析_生記録\週次走査_20261104\台帳.json`
  - `--out <dir>`: 出力ルート。既定 `C:\Users\M118A8586\Documents\製造データ解析_生記録\週次走査_<YYYYMMDD>\`(実行日で採番)
  - `--dry-run`: DB 計数と分類判定までを行い、確認用データの新規取得・登録・報告生成はしない(試運転・検証用)
  - `--only <分類>`: 走査対象を分類で絞る(`B,C,D` 等。省略時は B・C・D を全て走査し、E は契機のみ確認、A は対象外)
- **終了コード**: 正常終了 0。検出なしでも 0。致命的エラー(DB 接続不可・台帳不正)は 非0。
- **出力(1回の走査につき)**:
  - 走査ログ `週次走査_<YYYYMMDD>\走査ログ.md`
  - 更新後の台帳 `00_台帳.md` / `台帳.json`(最終走査日・次の契機を更新)
  - 検出時のみ 報告 `報告_<解析ID>.pptx` と `報告_<解析ID>.md`、および改善提案と追跡項目の台帳追記

---

## 2. 人が1コマンドで走らせる方法

### 2.1 Windows (PowerShell / コマンドプロンプト)

リポジトリ直下で、同梱の venv を使って実行する。

```bat
cd /d C:\Users\M118A8586\resonac-mcp
.venv\Scripts\python.exe scripts\weekly_scan.py
```

既定の台帳・出力先を使う場合は引数不要。出力先や台帳を変える場合:

```bat
cd /d C:\Users\M118A8586\resonac-mcp
.venv\Scripts\python.exe scripts\weekly_scan.py ^
  --ledger "C:\Users\M118A8586\Documents\製造データ解析_生記録\週次走査_20261104\台帳.json" ^
  --out "C:\Users\M118A8586\Documents\製造データ解析_生記録"
```

試運転(DB 計数・分類判定まで、登録や報告生成をしない):

```bat
cd /d C:\Users\M118A8586\resonac-mcp
.venv\Scripts\python.exe scripts\weekly_scan.py --dry-run
```

### 2.2 Linux / cron 相当(CI・検証環境)

```sh
cd ~/resonac-mcp
.venv/bin/python scripts/weekly_scan.py --ledger "$LEDGER" --out "$OUTDIR"
```

---

## 3. スケジューラへ登録する手順(登録はオーナーが決める。ここでは記述のみ)

> 以下は手順の記述であり、本書の作業では登録を実行しない。

### 3.1 Windows タスクスケジューラ

毎週月曜 08:00 に走らせる例。管理者 PowerShell で:

```powershell
$action  = New-ScheduledTaskAction `
  -Execute 'C:\Users\M118A8586\resonac-mcp\.venv\Scripts\python.exe' `
  -Argument 'scripts\weekly_scan.py' `
  -WorkingDirectory 'C:\Users\M118A8586\resonac-mcp'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 08:00
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
Register-ScheduledTask -TaskName 'ResonacWeeklyScan' `
  -Action $action -Trigger $trigger -Principal $principal `
  -Description '製造データ解析 週次走査'
```

GUI で登録する場合: タスクスケジューラ → 基本タスクの作成 → トリガー「毎週・月曜 08:00」→ 操作「プログラムの開始」→ プログラム `C:\Users\M118A8586\resonac-mcp\.venv\Scripts\python.exe`、引数 `scripts\weekly_scan.py`、開始場所 `C:\Users\M118A8586\resonac-mcp`。

登録の確認・削除:

```powershell
Get-ScheduledTask -TaskName 'ResonacWeeklyScan'
# 削除する場合
Unregister-ScheduledTask -TaskName 'ResonacWeeklyScan' -Confirm:$false
```

### 3.2 cron 相当(Linux)

毎週月曜 08:00。`crontab -e` に追記:

```cron
0 8 * * 1 cd $HOME/resonac-mcp && .venv/bin/python scripts/weekly_scan.py >> $HOME/resonac-mcp/logs/weekly_scan.log 2>&1
```

---

## 4. 走査後の確認項目

1. 出力ルートに `週次走査_<当日>\走査ログ.md` が生成されたか。
2. 台帳(`00_台帳.md` / `台帳.json`)の「最終走査」日が当日に更新されたか。B・C の「次の契機」(規定数到達予定日)が最新化されたか。
3. 検出(順序成立の採用、または強い候補)があれば `報告_<解析ID>.pptx` と `報告_<解析ID>.md` が生成され、改善提案と追跡項目が台帳に追記されたか。
4. A 以外の分類(B・C・D・E)が台帳から消えていないこと(取り逃し防止)。

---

## 5. トラブル時

- DB 接続不可 → 走査は非0終了。台帳は更新しない。接続情報を確認し再実行。
- 台帳 JSON 不正 → 非0終了。`台帳.json` の破損を確認し、直近のバックアップから復旧して再実行。
- 途中失敗 → 台帳の「最終走査」が当日未更新の解析は未走査。原因解消後に再実行すれば未走査分から再開する。
