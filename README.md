# m365-copilot-companion-mcp

> Microsoft 365 Copilot に **手** を生やすやつ。
> あなたの貸与ノート PC の上で動く。**100+ ツール**（執筆時点で 117）、
> 追加課金ゼロ、構築おおむね 1 人日。

🇯🇵 **このページは日本語版です。English version follows below ↓**

> **読むのがしんどい / 自分の環境で何が動くのか分からない場合**:
> この README 全文をコピーして、お使いの M365 Copilot か Claude に貼り付け、
> 「これは何で、自分の PC（OS・インストール済みアプリ）で実際に動くツールはどれ？」
> と聞いてください。AI のほうが、あなたの環境に合わせて噛み砕いてくれます。

---

## TL;DR

「M365 Copilot は Opus が中にいるのに、ファイルを読むくらいしかしてくれない」
を **物理的に** 解決する個人用 MCP サーバー。あなたの PC で動く Python サーバーが、Copilot に対して

- ファイルを読み・書き・整理する手
- Python を走らせる手（matplotlib でグラフも引ける）
- Excel / CSV / JSON を扱う手
- PowerPoint / Word / PDF を生成・読解する手
- ローカル / 社内の SQL を Windows 認証で叩く手
- バックグラウンドジョブを管理する手・スケジューラに登録する手
- そして **自分の出力を画像として読み返して自己検証する手**

を一式提供する。追加課金ゼロ、Microsoft 365 Copilot ライセンス内で完結。

```
[ M365 Copilot ]  ──▶  [ Copilot Studio エージェント ]  ──▶  [ Dev Tunnel ]
                                                                  ↓
                                  [ m365-copilot-companion-mcp on あなたのノート PC ]
                                                                  ↓
                                                  ファイル · Python · DB · Office 生成
```

> **ツール数について正直に**: `main.py` には 117 個のツールが登録されています。ただし
> **クローン直後に全部が動くわけではありません**。Outlook 本体・PowerPoint・Tesseract・
> ODBC ドライバ・Windows 環境などを前提とするものが含まれます。**いま自分の環境で実際に
> 有効なツールは `list_my_tools` を叩けば分かります**。前提条件は下のカタログにタグで明記しています。

> **法的な但し書き**: 本リポジトリは Microsoft Corporation とは無関係です。
> "Microsoft 365", "Copilot", "Copilot Studio" は各社の商標であり、本書では
> この companion がどのプロダクトに接続するかを説明する目的でのみ言及しています。

---

## ⚠️ 会社の PC で動かす前に、まず深呼吸

これは **あなたが自分で所有・管理するマシン** で、**あなたが既にアクセス権を持っているデータ** に対して、**あなた個人の用途** で動かすツールです。SaaS でもホスティングサービスでもなく、サポート契約もありません。`m365-` で始まる名前は付いていますが、**Microsoft 製ではありません**。

以下のいずれか 1 つでも当てはまる組織なら、**会社のノート PC には絶対にデプロイしないでください**:

- 個人用 MCP サーバーの稼働が禁止されている
- Microsoft Dev Tunnels が承認されていない
- Copilot Studio のエージェントは IT 管理のテンプレートからしか作れない
- 任意の Python パッケージを会社 PC に入れることが禁止されている
- AI のツール実行・エージェントのファイルアクセス・サードパーティ GitHub クローンを「セキュリティインシデント」と扱う運用がある

該当するなら、本 README は **アイデアの参考** として読むだけにして、正式な社内承認を通したうえで類似のものを組むのが筋です。Bearer キーを会社の Copilot Studio にコピペして「バレないだろう」は、後で必ず痛い目を見ます。

ライセンスは **MIT**。**無保証・無責任・無サポート**。本リポジトリの作者・コントリビュータは、あなたが運用上やらかした事故に対して **一切の責任を負いません**。特に `MCP_UNLOCK_PASSWORD` の取り扱いには細心の注意を。

> ひとこと添えると: 「Cowork（社内導入された M365 系・Claude 系のエージェント基盤の総称）
> は禁止されたが、Microsoft の純正製品なら問題ないだろう」というロジックが通る職場であれば、
> 本ツールにも目はあります。ただし運用は必ず **明示的に承認を取る** ことを原則としてください。
> 「禁止されていない」≠「許可されている」を取り違えると、いずれ然るべきタイミングで然るべき方からお声がかかります。

---

## 🧱 設計思想 — どこまでがこの companion の仕事か

これが本リポを理解する一番の近道です。**役割分担を最初に頭に入れてください。**

```
┌─────────────────────────────────────────────────────────────┐
│  M365 クラウド側                                              │
│  メール送受信 / 予定表 / Teams 投稿 / SharePoint 検索 / Web 検索 │
│      ↑ これは Copilot Studio が公式に用意している「純正コネクタ」 │
│        を自分でオンにするのが本筋。認証も監査も Microsoft 管理。  │
├─────────────────────────────────────────────────────────────┤
│  あなたのローカル PC 側                                        │
│  ファイル / Python 実行 / ローカル・社内 DB / Office 生成 / シェル │
│      ↑ ここが「Copilot Studio のコネクタでは届かない領域」。     │
│        この MCP サーバーが担当するのは ここ。                    │
└─────────────────────────────────────────────────────────────┘
```

つまり本来の使い方は **「Copilot Studio の純正コネクタ（クラウド側）」＋「この companion（ローカル側）」の二枚重ね** です。メールやカレンダー、SharePoint をエージェントに触らせたいなら、まず Copilot Studio 側でその純正コネクタをオンにするのが正解。Microsoft が認証・監査を持ってくれるので、社内承認も通しやすい。

**では、なぜ本リポに `outlook_*`（メール/カレンダー）や `web_search` があるのか？**
これらは **「純正コネクタを設定できない／したくない環境向けの、ローカル退避路」** です。`outlook_*` は Graph API の登録なしに、いま PC にログインしている Outlook を COM 経由で借りるだけ。`web_search` も Copilot 標準の Web 検索が使えるなら不要です。**クラウド連携の本命ではなく、塞がれている時の代替手段** と理解してください。重複して見えるツールは、この事情によるものです。

迷ったら原則: **クラウドで完結する仕事は純正コネクタ、ローカル PC を触る仕事はこの companion。**

---

## 🎯 何ができるか

`main.py` の `TOOLS` タプルに登録された関数群が、すべて MCP ツールとして公開されます。LLM はツールごとの docstring を読んで、必要に応じて自分で選びます。実行中は `list_my_tools` で **いまの環境で有効な** カタログを引けます。

**前提タグの凡例**:
🟢 追加要件なし（Python 依存のみ、クローン後すぐ動く） /
🪟 Windows 専用 /
📦 追加インストールが必要（下の「前提」参照） /
☁ 実行すると外部サービスにデータが出る

| カテゴリ | 主なツール | 前提 | 何のために |
|---|---|---|---|
| **コード実行 (Python / shell)** | `run_python`, `shell_exec`, `run_python_in_background`, `run_in_background`, `job_wait`, `job_status`, `job_output`, `job_list`, `job_kill` | 🟢 | コードを走らせる、長いやつは投げて待つ、暴走したら殺す |
| **PowerShell 専用** | `pwsh_exec`, `pwsh_exec_file`, `shell_which` | 🪟 | `cmd.exe` 経由ではなく PowerShell 5.1 / 7 を直接叩く（`-NoProfile -NonInteractive -ExecutionPolicy Bypass` 込み） |
| **プロセス / サービス / レジストリ** | `process_list`, `process_info`, `process_kill`, `service_status`, `registry_read` | 🪟📦`psutil` | タスクマネージャ + サービス + レジストリの読み口（kill は unlock 必須） |
| **ファイル I/O** | `read_file`, `write_file`, `append_file`, `list_directory`, `glob`, `find_files`, `copy_path`, `move_path`, `trash_path`, `create_directory`, `delete_path` | 🟢 | 許可ディレクトリ内のファイルを自在に。`trash_path` はゴミ箱送りで復元可 |
| **ファイル法医学** | `hash_file`, `find_duplicates`, `dir_size`, `file_metadata` | 🟢 | 「80 GB どこいった？」を 1 プロンプトで解明 |
| **編集・検索** | `grep`, `replace_in_file`, `multi_edit`, `diff_files`, `python_check` | 🟢 | 原子的な複数編集。「ファイル半分食われた」事故が起きない |
| **Git** | `git_status`, `git_diff`, `git_log`, `git_branch`, `git_blame`, `git_add`, `git_commit`, `git_checkout` | 📦`git` | 読みも書きも。`git_blame` でツッコむ相手を特定 |
| **表 / JSON** | `read_excel`, `write_excel`, `summarize_table`, `read_json`, `write_json` | 🟢 | Excel / CSV / JSON を一級市民として扱う |
| **PDF** | `read_pdf`, `pdf_info` | 🟢 | デジタル PDF のテキスト抽出 |
| **OCR** | `ocr_image`, `ocr_pdf` | 📦`Tesseract`（+`Poppler`） | スキャン画像/PDF の文字起こし。**read_image で Opus に直接読ませる手もある** |
| **画像（自己検証）** | `read_image`, `image_info` | 🟢 | エージェントが自分で作った図を **見返して** 確認できる |
| **PowerPoint** | `create_pptx`, `pptx_from_markdown`, `pptx_info`, `pptx_add_slide`, `pptx_add_image`, `pptx_add_table`, `pptx_replace_image` | 🟢 | スライド生成・画像/表埋込 |
| └ PNG 自己確認 | `pptx_export_png` | 🪟📦`PowerPoint 本体` | 各スライドを PNG 化してエージェントが目視 |
| **Word (.docx)** | `create_docx`, `docx_from_markdown`, `docx_info`, `read_docx` | 🟢 | 文書生成と読解。markdown 一発から正式文書まで |
| **Outlook (mail / calendar)** ※ローカル退避路 | `outlook_inbox`, `outlook_send_mail`, `outlook_calendar`, `outlook_create_event` | 🪟📦`Outlook 本体` | Graph コネクタが使えない時用。COM で今ログイン中の Outlook を借りる。送信は既定で「下書き保存」 |
| **クリップボード / スクリーン** | `clipboard_get`, `clipboard_set`, `screenshot` | 🪟 (screenshot は GUI セッション) | 「いまコピーしたこれ見て」「いま画面に映ってるもの撮って」 |
| **図 / 数式** | `render_diagram`(mermaid 等) ☁, `render_mermaid_png` ☁, `render_math` 🟢 | ☁`render_diagram`系は [Kroki](https://kroki.io) に送信 / `render_math` は外部不要 | アーキ図と数式を生成。**社外秘は render_diagram に出さない** |
| **Web** | `web_fetch`, `web_search`, `web_search_news`, `github_file` | ☁ | DuckDuckGo 検索・URL 取得。※Copilot 標準検索が使えれば不要 |
| **データベース (SQLite)** | `sqlite_tables`, `sqlite_schema`, `sqlite_query`, `sqlite_to_excel` | 🟢 | ローカル `.sqlite` を read-only で |
| **データベース (ODBC)** | `odbc_drivers`, `odbc_connections`, `odbc_tables`, `odbc_columns`, `odbc_query`, `odbc_to_excel` | 📦`ODBC ドライバ`+接続設定 | 社内 SQL Server / Azure SQL。Windows/Entra 認証継承、**read-only 強制** |
| **永続記憶** | `memory_save`, `memory_load`, `memory_list`, `memory_delete` | 🟢 | セッション横断のメモ。来週もエージェントが覚えてる |
| **スケジュール** | `schedule_create`, `schedule_list`, `schedule_info`, `schedule_run_now`, `schedule_delete` | 🪟 (タスクスケジューラ) | 「毎週金曜 9 時に週報生成」 |
| **ファイル監視** | `watcher_start`, `watcher_events`, `watcher_stop` | 📦`watchdog` | フォルダ変更検知 |
| **アーカイブ** | `zip_list`, `zip_extract`, `zip_create` | 🟢 | zip-slip 対策付き |
| **通知** | `notify_desktop` | 🪟 (トースト) | `job_wait` と組ませて「集計終わったよ」を能動通知 |
| **環境管理** | `env_info`, `pip_install`, `which`, `list_my_tools` | 🟢 | 実行環境の自己診断、足りないパッケージのその場 install |
| **セキュリティ** | `unlock`, `list_unlocked` | 🟢 | 変更系ツールの IP 単位パスワード解錠 |
| **その他** | `todo_write`, `todo_list`, `todo_clear` | 🟢 | エージェント自身の計画用スクラッチパッド |

> 「カタログに載ってるのに動かない」と感じたら、まず前提タグ（🪟 / 📦）を確認してください。
> 多くは依存をインストールするか Windows 上で動かせば解決します。どうしても要件を満たせない
> ツールは、その環境では単に使わなければよいだけで、他の 🟢 ツールには影響しません。

### ツールを増やすのは超簡単 — エージェント自身も増やせる

手動で増やす場合: `tools/*.py` に Python 関数を 1 つ書き、`main.py` の `TOOLS` タプルに追加して再起動するだけ。docstring が LLM の説明文になる。

そして本当に強力なのはここから。**エージェント自身が、その場で新しいツールを書けます**:

1. `pip_install` で必要なライブラリを入れる
2. `write_file` で `tools/your_ops.py` を生成する
3. `run_python` でロジックを動作確認する

——ここまでをチャットの中で完結できる。あとは `main.py` の `TOOLS` に 1 行足して再起動すれば、**自分で書いたツールが次回から常設ツールになる**。「◯◯の API を叩くツールが欲しい」と頼めば、エージェントが雛形を書いて検証し、登録手順まで提示します。つまりこのサーバーは **使いながら自分で育つ**。117 は出発点にすぎません。

---

## 🔁 自動リレー — Copilot を“裏で勝手に回す”（目玉機能）

ここがこのプロジェクトの一番尖った部分です。`relay/copilot_autopilot_relay.py` は、
**ゴールを1つ渡すと、あなたの M365 Copilot エージェントを自律的に完了まで駆動する**
スタンドアロンのコントローラ（中継器）です。

```
ゴール投入 ──▶ [ relay ] ──CDP──▶ [ Edge のあなたの Copilot タブ ]
                  ▲  完了検知して次を自動投入             │ MCP ツールで実作業
                  └───────────── 応答を読む ◀────────────┘
```

**何がすごいか:**

- **あなたの作業を一切邪魔しない。** ブラウザを Chrome DevTools Protocol 経由で
  駆動するため、**OS のマウスカーソルもキーボードフォーカスも奪いません**。
  relay が裏で Copilot 会話を回している間、あなたは別ウィンドウで普通に入力作業を
  続けられます。スクショ＋クリックの自動化（=画面を占有する）とは根本的に違います。
- **Claude も人間もループに不在。** 唯一の知能は Copilot エージェント自身（固定
  オラクル）。relay 側は「完了を検知して次の job を投げる」決定的な配管だけで、
  生成 AI を一切使いません。だから**完全無人で回り続けます**。
- **記録される。** 各ターンを **クロスセッション memory** と **監査ラン
  ログ（operator D）** の両方に保存。後から `runlog_summarize` で収束の軌跡を
  確認でき、次回の relay は前回の文脈を memory から引き継いで再開します。
- **止められる・問える。** `stop_request()`（kill-switch）を毎ターン＆長時間
  待機中もポーリング。HITL ゲートと組み合わせれば要所で人間に確認を取れます。
- **完了でも停滞でも必ず通知。** ゴール達成（DONE）・行き詰まり（STUCK）・
  上限到達（MAXTURNS）・中止（ABORTED）のいずれでも `notify_desktop` でデスクトップ
  通知。重いタスクを投げて別作業に戻り、終わった/止まった時だけ気づけます。
  停滞は「無進捗が続く・ターンがタイムアウト・エージェントが STUCK 申告」で自動検知。

> 制御ループの信頼性は `relay/test_relay_loop.py` で**全終了パスを検証済み**
> （DONE / 無進捗STUCK / FAIL→自己修復 / タイムアウトSTUCK / エージェントSTUCK /
> MAXTURNS / kill-switch の 7 シナリオ、各々で通知発火を確認）。ブラウザ無しで
> モックドライバにより決定的にテストできます。

**一回だけのセットアップ**（再ログイン不要・Playwright のブラウザ DL も不要 ―
既にログイン済みの Edge に attach するだけ）:

```powershell
.\.venv\Scripts\pip.exe install -r requirements-relay.txt

# Edge を debug ポート付きで起動（Chrome でも可）
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222
# → その Edge で M365 Copilot を開き、MCP エージェントで新規チャットを開始し、
#   会話 URL をコピー

.\.venv\Scripts\python.exe relay\copilot_autopilot_relay.py `
  --conversation-url "https://m365.cloud.microsoft/chat/agent/.../conversation/..." `
  --goal "copilot_loop_demo に data.csv(10行)を作り、合計と平均を出す stats.py を書き、self-test を足して PASS させ、SUMMARY にまとめる" `
  --max-turns 12
```

> セレクタはライブの M365 Copilot DOM から採取済み（`COPILOT_SELECTORS` に隔離）。
> Microsoft が DOM を変えたらそこだけ直せば動きます。

---

## 🚀 セットアップ（あなた個人の PC で）

### 0. 前提

- **Windows 10 / 11**（PowerShell 5+）。多くは macOS / Linux でも動きますが、🪟 タグのツール（PowerShell・プロセス・レジストリ・スケジューラ・通知・Outlook・スクリーン）は Windows 専用
- **Python 3.10 以降**（3.11 推奨）
- **Git**
- 任意（対応するツールを使うときだけ）:
  - **Tesseract OCR** + 言語データ（`ocr_*` 用。日本語は `jpn.traineddata`、[UB-Mannheim ビルド](https://github.com/UB-Mannheim/tesseract/wiki)）
  - **Poppler**（`ocr_pdf` 用、PATH に）
  - **Microsoft PowerPoint 本体**（`pptx_export_png` の COM エクスポート用）
  - **Microsoft Outlook 本体**（`outlook_*` 用）
  - **ODBC Driver 18 for SQL Server**（`odbc_*` で社内 DB に繋ぐなら）

### 1. クローン & 仮想環境

```powershell
git clone https://github.com/MasayukiTa/m365-copilot-companion-mcp.git
cd m365-copilot-companion-mcp
```

**ワンクリック セットアップ（推奨）** — venv 作成・依存インストール・`.env`（ランダム秘密入り）自動生成までを一括:

```powershell
.\setup.ps1
# 外部ツール(devtunnel + Tesseract OCR)も winget で入れるなら:
.\setup.ps1 -WithExternalTools
```

手動でやる場合:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. `.env` を作る

```powershell
Copy-Item .env.example .env
notepad .env
```

中身を **個人ごとに新規生成** して貼る（絶対に他人と共有しないこと）:

```powershell
python -c "import secrets; print('MCP_API_KEY=' + secrets.token_hex(20))"
python -c "import secrets; print('MCP_UNLOCK_PASSWORD=' + secrets.token_hex(8))"
```

`MCP_ALLOWED_BASE` を `~/work` のようなサブフォルダに絞ると、より安全になります（デフォルトはホーム全体）。

### 3. サーバー起動

```powershell
.\start.ps1
```

`http://127.0.0.1:8000/mcp` で待受開始（Streamable HTTP transport）。

### 4. 外から見えるようにする（リモートクライアント用）

ローカルの Claude Desktop だけなら、この step は不要です。

Microsoft 365 Copilot Studio から繋ぐなら、`localhost:8000` を HTTPS で公開する必要があります。最も簡単な無料手段が
[Microsoft Dev Tunnels](https://learn.microsoft.com/azure/developer/dev-tunnels/) です:

```powershell
winget install Microsoft.devtunnel
devtunnel user login                                # 初回のみ
devtunnel create m365-copilot-companion --allow-anonymous
devtunnel port create m365-copilot-companion -p 8000 --protocol http
devtunnel host m365-copilot-companion
# → https://<random>-8000.<region>.devtunnels.ms
```

`--allow-anonymous` でも安全な理由は、本サーバーが **Bearer API キー** と **IP 単位の unlock** を必須にしているから。URL を当てずっぽうで叩いてきた人間 / bot は即 401 で弾かれます。

### 5. MCP クライアントに登録

**Microsoft 365 Copilot Studio:**

1. エージェントを作成または既存のを開く
2. ツール → 「ツールを追加」 → 「Model Context Protocol」
3. サーバー URL: `https://<your-tunnel>-8000.<region>.devtunnels.ms/mcp`
4. 認証: 「API キー（手動）」、ヘッダ名 `Authorization`、ヘッダ値 `Bearer <your MCP_API_KEY>`
5. 保存 → 「接続を追加」 → ツール一覧が読み込まれれば成功

> このとき同時に、Copilot Studio の **純正コネクタ**（メール・予定表・Teams・SharePoint など）も
> 必要に応じてオンにしておくと、クラウド側はそちら・ローカル側はこの companion、という
> 本来の二枚重ねになります（→「🧱 設計思想」参照）。

**配布範囲は必ず「自分のみ」**。「組織全体」を選ぶ前に、エージェントが何を触れるか完全に把握してください。

**Claude Desktop / Claude Code:**

```json
{
  "mcpServers": {
    "companion": {
      "transport": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <your MCP_API_KEY>" }
    }
  }
}
```

### 6. 動作確認

エージェントに:

> 「`list_my_tools` を呼んで。」

ツール一覧が返れば配線 OK。返ってきた中で 🟢 のものはすぐ使えます。🪟 / 📦 のものは、対応する OS / アプリ / ライブラリが揃っていないと、呼んだときに「その依存が無い」旨のエラーを返します（サーバー全体は落ちません）。読み取り系はそのまま、書込・実行系を初めて使うと:

> `[locked client IP: '203.0.113.42'] Call unlock(password='...') first.`

と言われます。指示通り `unlock(password="<your MCP_UNLOCK_PASSWORD>")` を呼ぶと、その IP が `MCP_UNLOCK_TTL_DAYS` 日（既定 30 日）解錠されます。

---

## 🔐 セキュリティモデル

3 層で重ねます。順に通過しないと先に進めません:

| 層 | 仕組み | 失敗時 |
|---|---|---|
| **認証** | 固定 Bearer トークン（`MCP_API_KEY`、40 桁のランダム hex） | 401 Unauthorized |
| **場所の認可** | 全ファイルアクセスは `_validate_path` を通過。`MCP_ALLOWED_BASE` 配下以外はブロック | `PermissionError` |
| **行為の認可** | 書込 / 実行系は `require_unlocked()` を呼ぶ。`X-Forwarded-For` か socket peer から取った IP をホワイトリストと突き合わせ（TTL あり） | "locked" メッセージで `unlock(password)` を要求 |

知っておくべきこと:

- unlock パスワードは API キーとは **別物**。どちらか片方の漏洩だけでは変更系は通せません
- `127.0.0.1` / `::1` は自動的に信頼（ローカル開発用）
- ODBC は `readonly=True` で接続、`SELECT / WITH / EXEC / SHOW / DESCRIBE` のみ許可
- DB 認証は Windows / Entra 統合認証なので、エージェントは **あなたの既存権限の範囲でしか SQL を投げられません**。DBA への新規アカウント申請は不要
- Bearer キーのローテーションは `.env` 編集 → 再起動だけ。古いキーは即死

これは **何ではないか**:

- ハードン済みのマルチテナントサービスではない
- ペネトレーションテスト済みではない
- あなたの PC を既に乗っ取った攻撃者からは何も守れない（その攻撃者は既にエージェントと同等以上の権限を持っている）
- 同僚に unlock パスワードを教えてしまうあなた自身からは守れない。**教えないでください**

---

## 🛟 キラーフィーチャー: 自己検証ループ

エージェント系の事故の大半は「完了したと言ったけど実は何もしていない」「画像が入っていない pptx を作って完了報告した」。本サーバーはエージェントが自分の出力を **見返す** ための 2 つのツールを持ちます:

- `read_image(path)`: PNG/JPG を base64 data URI として返す。Vision 対応モデル（M365 Copilot 内の Opus も含む）はそのまま読める
- `pptx_export_png(pptx_path)`: PowerPoint を COM 経由で起動し、各スライドを PNG にエクスポートする（🪟 PowerPoint 本体が必要）。`create_pptx` の後にこれを呼んで、エージェント自身が「画像が入っているか」「日本語が豆腐化していないか」を確認する

典型的な「Trust but Verify」ループ:

```
run_python       → chart.png を保存
read_image       → エージェントが目視確認、ズレてたら直す
create_pptx      → chart.png を report.pptx に埋め込む
pptx_export_png  → 各スライドの PNG をエージェントが流し見
notify_desktop   → 「report.pptx 完成」
```

system prompt にこの順序を 1 行入れておけば「画像なし pptx を完成と報告」事故は消えます。

---

## 📁 リポジトリ構成

```
m365-copilot-companion-mcp/
├── main.py                  # FastMCP のエントリポイント、ツール登録
├── start.ps1                # 起動スクリプト（.venv 自動検出）
├── requirements.txt
├── .env.example             # コピーして .env を作る
├── .gitignore               # 秘密・ランタイム状態・業務データを除外
├── LICENSE                  # MIT
│
├── tools/
│   ├── code_exec.py         # run_python, shell_exec
│   ├── shell_extra.py       # 🪟 pwsh_exec / pwsh_exec_file / shell_which
│   ├── jobs.py              # 非同期ジョブ管理
│   ├── process_ops.py       # 🪟 process_list / process_info / process_kill
│   ├── registry_ops.py      # 🪟 registry_read / service_status
│   ├── file_ops.py          # ファイル I/O + 法医学
│   ├── search_ops.py        # glob, find_files
│   ├── archive_ops.py       # zip 操作
│   ├── coding_ops.py        # grep, multi_edit, git_*, diff_files, python_check
│   ├── data_ops.py          # Excel / CSV / JSON
│   ├── pdf_ops.py           # PDF テキスト抽出
│   ├── ocr_ops.py           # 📦 Tesseract ラッパー
│   ├── image_ops.py         # read_image, image_info（自己検証）
│   ├── pptx_ops.py          # PowerPoint 生成（pptx_export_png は 🪟 COM）
│   ├── docx_ops.py          # Word (.docx) 生成・読解
│   ├── outlook_ops.py       # 🪟 Outlook COM（ローカル退避路）
│   ├── clipboard_ops.py     # クリップボード読み書き
│   ├── screenshot_ops.py    # 🪟 スクリーンキャプチャ
│   ├── diagram_ops.py       # ☁ Kroki ベースの図生成
│   ├── render_ops.py        # matplotlib mathtext の数式レンダラ
│   ├── web_ops.py           # ☁ web_fetch, github_file
│   ├── search_web.py        # ☁ DuckDuckGo 検索
│   ├── sql_ops.py           # SQLite（read-only）
│   ├── odbc_ops.py          # 📦 ODBC / SQL Server / Azure SQL
│   ├── schedule_ops.py      # 🪟 Windows タスクスケジューラ
│   ├── watcher_ops.py       # 📦 フォルダ監視（watchdog）
│   ├── memory_ops.py        # クロスセッション記憶
│   ├── notify_ops.py        # 🪟 Windows トースト
│   ├── env_ops.py           # env_info, pip_install, which
│   ├── task_ops.py          # todo_write / list / clear
│   ├── registry.py          # @register デコレータと list_my_tools
│   └── security.py          # unlock / require_unlocked / IP ホワイトリスト
│
└── agent_memory/            # 長期メモ（大半は git 除外）
    ├── README.md            # 追跡: スキーマ説明
    └── templates/           # 追跡: 空テンプレート
```

---

## 🧩 推奨システムプロンプト断片

Copilot Studio または Claude Desktop のエージェント instructions に貼り付け推奨:

```
あなたは companion の操作者。companion はユーザーの PC 上で動き、多数の
MCP ツールを公開している。

- 何が使えるか不明な時は最初に list_my_tools を呼ぶ。ツールが「依存が無い」
  エラーを返したら、ユーザーにその前提（OS/アプリ/ライブラリ）を伝えて代替手段を
  提案する。全部のツールがどの環境でも動くわけではない。
- 読取系は常に使える。書込・実行系は IP 単位で unlock(password) を必要とする。
  ロック解除を求められたらユーザーに 1 度だけ伝えればよい。
- クラウド側の作業（メール・予定表・Teams・SharePoint）は、可能なら Copilot Studio の
  純正コネクタを使う。この companion の outlook_* 等はコネクタが使えない時の代替路。
- 出力ファイルは原則 ~/Desktop/<案件名>/ 配下に保存する。
- 画像や PowerPoint を生成した直後は read_image / pptx_export_png で自己検証し、
  画像欠落・豆腐化・レイアウト崩れがあれば最大 3 回まで修正と再エクスポートをループする。
- 重い処理は run_python_in_background + job_wait で投げ、終わったら
  notify_desktop でユーザーに能動通知する。ユーザーをスピナーで待たせない。
- ユーザーや案件について長期的に役立つ情報を知ったら memory_save する。
  既出の話題には memory_load / memory_list で取りに行ってから回答する。
- 必要なツールが無ければ、pip_install + write_file + run_python で自作してよい。
  動作確認後、main.py の TOOLS への登録手順をユーザーに案内する。
- 機密データを外部サービス（Kroki, web_fetch 等）に流さない。迷ったらローカル処理。
```

---

## 🛠 トラブルシュート

> **🔌 まず最初に疑うこと — Copilot がこう言い出したら、ほぼ MCP/トンネル切れです:**
> 日本語: **「申し訳ございません。それに応答できませんでした。他に何かお手伝いできることはありますか?」**
> 英語: （同等の "Sorry, I couldn't respond to that…" 系。正確な文言は未確認）
>
> この文言が出たら、エージェントの故障ではなく **MCP サーバーか Dev Tunnel が落ちている**
> 可能性が高い。確認順:
> 1. ローカルでサーバー生存: `Test-NetConnection localhost -Port 8000`
> 2. トンネルが host 中か: `devtunnel show <tunnel> | Select-String "Host connections"`（**0 なら切れ**。プロセスは生きていても host 接続だけ落ちることがある）
> 3. supervisor が動いているか（下記「常時起動」参照）。動いていれば数十秒で自動復活する
> 4. 手動復旧: サーバー起動 → `devtunnel host <tunnel>`

### 常時起動 / 切断対策（supervisor）

Dev Tunnel の host 接続は、**プロセスが生きていても relay 接続だけが静かに落ちる**ことがあります
（これが上の Copilot エラーの主因）。同梱の `supervisor.ps1` は、ポート 8000 とトンネルの
`Host connections` を定期監視し、落ちていれば自動で張り直します（誤検知防止のデバウンス＋
接続確立待ち付き）。

手動で起動:

```powershell
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass `
  -File .\supervisor.ps1 -TunnelName <あなたのトンネル名>
```

**ログオンのたびに自動起動**させたい場合（管理者権限不要。Task Scheduler が組織ポリシーで
弾かれる環境でも通る方法）— **スタートアップ フォルダ**にランチャを置く:

```powershell
$startup = [Environment]::GetFolderPath('Startup')
$root = (Get-Location).Path
@"
@echo off
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "$root\supervisor.ps1" -TunnelName <あなたのトンネル名>
"@ | Set-Content -Encoding ASCII (Join-Path $startup "start-companion-supervisor.cmd")
```

これで再起動・スリープ復帰後もログオン時に supervisor が立ち上がり、サーバー＋トンネルを
復活させます（多重起動は内部の mutex で防止）。

| 症状 | 対処 |
|---|---|
| ツール一覧に出るのに呼ぶとエラー | そのツールの前提タグ（🪟 / 📦）を確認。対応する OS / アプリ / ライブラリを入れるか、その環境では使わない |
| `odbc_*` が接続不可 | ODBC Driver 18 for SQL Server をインストール、`odbc_drivers` で確認 |
| `ocr_*` が空を返す | Tesseract と言語データ（`jpn.traineddata` 等）を入れて `which("tesseract")` で確認。または read_image で Opus に直接読ませる |
| `pptx_export_png` 失敗 | ホスト PC に Microsoft PowerPoint がインストールされている必要あり（COM 経由） |
| `outlook_*` 失敗 | ホストに Outlook 本体が必要。なければ Copilot Studio の純正メールコネクタ側でやる |
| `render_diagram` で SSL エラー | 社内プロキシが Kroki をブロック。CA bundle を直すか、本ツールを使わず matplotlib でローカル描画させる |
| Copilot Studio がタイムアウト | 1 リクエストの実質予算は ~90 秒。`run_in_background` → `job_wait` で分割 |
| `unlock` を何度も要求される | 呼び出し元 IP が変わった（VPN 切替、Copilot Studio バックエンドの hop）。再 unlock |

---

## 🇯🇵 よくある質問（日本企業で運用する人向け）

**Q. このツール、結局なにをしてくれるの？（Cowork との違いは？）**
A. 「🧱 設計思想」の役割分担がそのまま答えです。Cowork（社内導入された M365 系・Claude 系エージェントの総称）が **何を指すか・なぜ禁止になったか** は会社ごとに違います。この companion がやるのは **「あなたが使ってよい Copilot に、ローカル PC の手を足す」** こと。クラウドで完結する作業（メール・予定表・SharePoint）は Copilot Studio の純正コネクタに任せ、この companion は **Copilot のコネクタでは届かない自分の PC の中身**（ファイル・Python・ローカル DB・Office 生成）を担当します。

**Q. 情シスに何と説明すれば？**
A. 「自分の貸与 PC 上で動く個人ツールで、自分が既にアクセス権を持つローカルリソースにしかアクセスしません」が正確。`MCP_ALLOWED_BASE` で許可パスを絞っていること、ODBC は read-only で Windows 認証を継承していること、クラウド側は Microsoft 純正コネクタに寄せていることを併せて伝えると話が早い。それでも NG なら、潔く諦めましょう。

**Q. 上司に「これ何？」と聞かれた**
A. 冒頭の図と「🧱 設計思想」の二段重ねの図を見せて「Copilot に手を生やすやつ。クラウドは純正コネクタ、ローカル PC はこれ」で多くは伝わる。技術系の上司なら「Dev Tunnel で localhost に MCP」まで言えば終わり。事務系の上司なら「Excel の集計を Copilot に頼んだら本当にやってくれる」が刺さる。

**Q. 監査でバレないか？**
A. **バレます**。隠す前提で運用しないでください。Dev Tunnel のログは Microsoft に残り、Copilot Studio の操作履歴も残ります。最初から「会社の AI 活用の試行として、自分の PC で動かしている」と説明できる形で運用してください。

**Q. 同僚にも使わせたい**
A. それは「共有」「公開」になります。Copilot Studio のチャネル設定で「自分のみ」を外す前に、情シスとの会話を 1 度挟んでください。共有用途は本リポの想定範囲外です。

**Q. データが Microsoft に流れない？**
A. M365 Copilot のチャット内容と、エージェントが返した文字列は Microsoft の Copilot Studio に流れます。これはエージェントを Copilot Studio で動かす以上、避けようがありません。ローカルファイルの中身も、エージェントが回答に含めればそのままチャットに出ます。**取り扱い注意の情報は最初からエージェントに見せないこと**。`MCP_ALLOWED_BASE` を限定するのは、この事故を物理的に減らすための仕切りです。なお `render_diagram`（Kroki）や `web_fetch` は ☁ タグの通り外部に出るので、社外秘では使わないこと。

**Q. README の内容が難しい / 自分の環境で何が動くか分からない**
A. この README 全文をコピーして、お使いの M365 Copilot か Claude に貼り、「これは何で、自分の OS・インストール済みアプリで実際に動くツールはどれ？」と聞いてください。AI があなたの環境前提で噛み砕いてくれます。

**Q. 退職時はどうする？**
A. `.env` の API キーと unlock パスワードは破棄。Dev Tunnel と Copilot Studio エージェントは削除。`.unlock_state.json` `.memory_state.json` `.todo_state.json` `agent_memory/` 配下も削除。PC 返却時にこの掃除は必須です。

---

## 🤝 コントリビュート

PR や Issue 歓迎。お願い:

- ツールの docstring は短く具体的に。**LLM が読みます**、人間だけが読むわけではありません
- 変更系ツールは必ず `tools/security.py` の `require_unlocked()` をラップ
- ディスクアクセスは必ず `_validate_path` を通して `MCP_ALLOWED_BASE` を越えないように
- OS / アプリ / ライブラリの前提があるツールは、その旨を docstring と README カタログのタグに明記
- **絶対にコミットしないもの**: `.env`, `.unlock_state.json`, `.memory_state.json`, 業務データ。`.gitignore` で塞いであります、緩めないでください
- 新ツールが外部サービスを呼ぶ場合、何のデータがどこに出ていくか docstring に明記（☁ タグ）。運用者がオプトアウトできるように

## 📜 ライセンス

[MIT](./LICENSE)。自己責任で自由にどうぞ。上の警告を頭に置いたうえで。

---
---

# 🇺🇸 English version

> Microsoft 365 Copilot shipped you a brain. It forgot the hands.
> This is the hands.

A personal-use **Model Context Protocol (MCP) server** that turns one
laptop into a fully-capable agent backend for **Microsoft 365 Copilot**,
**Claude Desktop**, or any other MCP-aware client. **100+ tools** (117 at
the time of writing). Zero external API keys. Built in roughly a day.

> **If this README is long or you're not sure what runs on your machine**:
> copy the whole thing into your M365 Copilot or Claude and ask
> "what is this, and which tools actually work on my PC (my OS + installed
> apps)?" The model will tailor it to your environment.

The motivating frustration: corporate M365 Copilot licences come with
Claude Opus included, but Opus has no fingers. It can read what you
paste into the chat, and that's about it. This server gives the model
**real hands** on the one laptop where you have permission to do as you
please — your own.

```
[ M365 Copilot ]  ──▶  [ Copilot Studio agent ]  ──▶  [ Dev Tunnel ]
                                                            ↓
                                     [ m365-copilot-companion-mcp on your laptop ]
                                                            ↓
                                       your files · Python · DBs · Office gen
```

One user, one companion, one laptop. Nothing is centralised. Nothing
leaves the box you wouldn't want it to. Costs **zero** beyond the M365
Copilot licence you already have.

> **Honest note on the tool count**: `main.py` registers 117 tools, but
> **not all of them run on a fresh clone**. Some require Outlook,
> PowerPoint, Tesseract, an ODBC driver, or Windows. **Run `list_my_tools`
> to see what's actually live in your environment.** Requirements are
> tagged in the catalog below.

> Not affiliated with, endorsed by, or sponsored by Microsoft Corporation.
> "Microsoft 365", "Copilot", and "Copilot Studio" are trademarks of their
> respective owners; referenced only to describe what this attaches to.

---

## ⚠️ Before you go any further

This is a thing you run **on a machine you control**, against accounts
and data **you already have permission to touch**, for **your own use**.
It is not a SaaS, not a hosted service, no support contract, and the
friendly Microsoft-flavoured name does not change that.

If your employer blocks personal MCP servers, hasn't approved Dev
Tunnels, restricts Copilot Studio agents to IT-managed templates, forbids
arbitrary pip installs, or treats AI tool execution / agent file access /
third-party GitHub clones as a security incident — **do not deploy this on
a company laptop.** Read it for ideas, build your own through proper
channels.

The licence is MIT. No warranty, no obligation, no liability.
**Whatever you blow up on your side is your problem.** Mind the unlock
password.

---

## 🧱 Design philosophy — where this companion's job ends

This is the fastest way to understand the repo. **Keep the split in mind:**

```
┌─────────────────────────────────────────────────────────────┐
│  M365 cloud side                                              │
│  mail / calendar / Teams posts / SharePoint search / web      │
│      ↑ Turn on Copilot Studio's own first-party CONNECTORS.   │
│        Microsoft owns the auth and the audit trail.           │
├─────────────────────────────────────────────────────────────┤
│  Your local PC side                                           │
│  files / Python exec / local & corporate DBs / Office gen /   │
│  shell                                                        │
│      ↑ The part Copilot Studio connectors can't reach.        │
│        THIS is what the MCP server is for.                    │
└─────────────────────────────────────────────────────────────┘
```

The intended setup is **both layers stacked**: first-party connectors for
the cloud, this companion for the local box. If you want the agent to
touch mail, calendar, or SharePoint, the right move is to enable Copilot
Studio's native connector for it — Microsoft handles auth and audit, which
also makes it easier to get past IT.

**Then why does this repo ship `outlook_*` and `web_search`?** They are
**local fallbacks for when you can't or don't want to set up the native
connector.** `outlook_*` borrows the already-signed-in Outlook over COM,
no Graph registration. `web_search` is unnecessary if Copilot's built-in
web search is available. They are escape hatches, not the primary path.
That's why some tools look redundant with Microsoft's connectors.

Rule of thumb: **cloud-only work → native connector; touching your local
PC → this companion.**

---

## 🎯 What this thing can do

`main.py` registers a flat catalog. Call `list_my_tools` at runtime to see
what's **live in your environment**.

**Requirement legend:**
🟢 no extra requirement (Python deps only, works right after clone) /
🪟 Windows-only /
📦 needs an extra install (see Prereqs) /
☁ sends data to an external service when used

| Category | Tools | Req. | What it's for |
|---|---|---|---|
| **Code execution** | `run_python`, `shell_exec`, `run_python_in_background`, `run_in_background`, `job_wait`, `job_status`, `job_output`, `job_list`, `job_kill` | 🟢 | Run code. Wait. Kill the runaway. |
| **PowerShell** | `pwsh_exec`, `pwsh_exec_file`, `shell_which` | 🪟 | Dedicated PowerShell with `-NoProfile -NonInteractive -ExecutionPolicy Bypass` defaults. |
| **Processes / services / registry** | `process_list`, `process_info`, `process_kill`, `service_status`, `registry_read` | 🪟📦`psutil` | Task Manager + Services + Registry, read-side (kill needs unlock). |
| **File I/O** | `read_file`, `write_file`, `append_file`, `list_directory`, `glob`, `find_files`, `copy_path`, `move_path`, `trash_path`, `create_directory`, `delete_path` | 🟢 | Read, write, move, delete inside the allowed base. |
| **File forensics** | `hash_file`, `find_duplicates`, `dir_size`, `file_metadata` | 🟢 | "Where did 80 GB go?" in one prompt. |
| **Editing / search** | `grep`, `replace_in_file`, `multi_edit`, `diff_files`, `python_check` | 🟢 | Atomic multi-edit. |
| **Git** | `git_status`, `git_diff`, `git_log`, `git_branch`, `git_blame`, `git_add`, `git_commit`, `git_checkout` | 📦`git` | Reads and writes. |
| **Tabular / JSON** | `read_excel`, `write_excel`, `summarize_table`, `read_json`, `write_json` | 🟢 | First-class spreadsheet handling. |
| **PDF** | `read_pdf`, `pdf_info` | 🟢 | Digital PDF text extraction. |
| **OCR** | `ocr_image`, `ocr_pdf` | 📦`Tesseract`(+`Poppler`) | Scanned docs. Or just `read_image` and let Opus read it. |
| **Image (self-verify)** | `read_image`, `image_info` | 🟢 | The agent sees what it made. |
| **PowerPoint** | `create_pptx`, `pptx_from_markdown`, `pptx_info`, `pptx_add_slide`, `pptx_add_image`, `pptx_add_table`, `pptx_replace_image` | 🟢 | Build decks, embed charts/tables. |
| └ PNG self-check | `pptx_export_png` | 🪟📦`PowerPoint` | Render each slide to PNG to audit. |
| **Word (.docx)** | `create_docx`, `docx_from_markdown`, `docx_info`, `read_docx` | 🟢 | Author and read Word docs. |
| **Outlook** *(local fallback)* | `outlook_inbox`, `outlook_send_mail`, `outlook_calendar`, `outlook_create_event` | 🪟📦`Outlook` | For when the Graph connector isn't set up. COM over the signed-in Outlook. Sends to Drafts by default. |
| **Clipboard / screen** | `clipboard_get`, `clipboard_set`, `screenshot` | 🪟 (screenshot: GUI session) | "Look at what I just copied / what's on screen." |
| **Diagrams / math** | `render_diagram`☁, `render_mermaid_png`☁, `render_math`🟢 | ☁ (Kroki) / `render_math` local | Don't send confidential content to `render_diagram`. |
| **Web** | `web_fetch`, `web_search`, `web_search_news`, `github_file` | ☁ | DuckDuckGo + URL fetch. Skip if Copilot's own search works. |
| **DB (SQLite)** | `sqlite_tables`, `sqlite_schema`, `sqlite_query`, `sqlite_to_excel` | 🟢 | Local `.sqlite`, read-only. |
| **DB (ODBC)** | `odbc_drivers`, `odbc_connections`, `odbc_tables`, `odbc_columns`, `odbc_query`, `odbc_to_excel` | 📦 driver + config | Corporate SQL Server / Azure SQL. Windows/Entra auth, read-only. |
| **Persistent memory** | `memory_save`, `memory_load`, `memory_list`, `memory_delete` | 🟢 | Cross-session notes. |
| **Scheduling** | `schedule_create`, `schedule_list`, `schedule_info`, `schedule_run_now`, `schedule_delete` | 🪟 | "Every Friday at 9, regenerate the report." |
| **File watching** | `watcher_start`, `watcher_events`, `watcher_stop` | 📦`watchdog` | React to folder changes. |
| **Archives** | `zip_list`, `zip_extract`, `zip_create` | 🟢 | zip-slip protected. |
| **Notifications** | `notify_desktop` | 🪟 | Windows toast; pair with `job_wait`. |
| **Environment** | `env_info`, `pip_install`, `which`, `list_my_tools` | 🟢 | Introspect host; install deps on the fly. |
| **Security** | `unlock`, `list_unlocked` | 🟢 | Per-IP unlock for mutating tools. |
| **Misc** | `todo_write`, `todo_list`, `todo_clear` | 🟢 | Agent's planning scratchpad. |

> If a tool shows up but errors when called, check its requirement tag
> (🪟 / 📦) first. Install the dependency or run on Windows — or just don't
> use it in that environment. The 🟢 tools are unaffected.

### Extending it is trivial — and the agent can do it itself

Manual route: write a Python function in `tools/*.py`, add it to the
`TOOLS` tuple in `main.py`, restart. The docstring becomes the LLM's
description.

The powerful part: **the agent can author new tools on the fly.**
`pip_install` the library, `write_file` a new `tools/your_ops.py`,
`run_python` to test it — all inside the chat. Add one line to `TOOLS`,
restart, and the tool it just wrote becomes permanent. Ask "I want a tool
that calls X's API" and it'll scaffold, test, and hand you the
registration step. The server **grows as you use it.** 117 is the floor.

---

## 🔁 Autonomous relay — drive Copilot in the background (the headline feature)

The sharpest part of the project. `relay/copilot_autopilot_relay.py` is a
standalone controller (the "frame") that, given a single goal, **drives your
M365 Copilot agent to completion autonomously**.

```
goal ──▶ [ relay ] ──CDP──▶ [ your Copilot tab in Edge ]
            ▲  detect completion, inject next job        │ does the real work via MCP
            └──────────────── read the answer ◀──────────┘
```

**Why it matters:**

- **It does not interfere with your other work.** It drives the page through the
  Chrome DevTools Protocol, so keystrokes and clicks are dispatched into the tab
  **without moving your OS cursor or stealing keyboard focus**. The relay pumps a
  Copilot conversation in one tab while you keep typing in other windows. That is
  the whole point versus screenshot-and-click automation, which owns your screen.
- **No Claude and no human in the loop.** The only intelligence is the Copilot
  agent itself (the fixed oracle). The frame is deterministic plumbing -- detect
  completion, decide, inject the next job -- and makes zero model calls. So it
  runs fully unattended.
- **Everything is recorded.** Each turn is saved to **cross-session memory** and
  an **audit run-log (operator D)**. Inspect the convergence trajectory later with
  `runlog_summarize`; the next relay run resumes with context pulled from memory.
- **Stoppable and gateable.** `stop_request()` (kill-switch) is polled every turn
  and during long waits; combine with the HITL gate to ask a human at key points.
- **Always notifies -- on success AND on stall.** Whether the goal completes
  (DONE), gets stuck (STUCK), hits the turn cap (MAXTURNS) or is aborted
  (ABORTED), it fires a desktop notification via `notify_desktop`. Throw a heavy
  task at it, go do other work, and you only get pulled back when it finishes or
  stalls. Stall is auto-detected from: no progress across turns, a turn timing
  out, or the agent self-reporting STUCK.

> The control loop's reliability is **proven across every terminal path** in
> `relay/test_relay_loop.py` (7 scenarios: DONE / no-progress STUCK / FAIL->fix /
> timeout STUCK / agent STUCK / MAXTURNS / kill-switch, each asserting the
> notification fires) -- deterministically, with a mock driver, no browser needed.

**One-time setup** (no re-login, no Playwright browser download -- it attaches to
the Edge you are already signed into):

```powershell
.\.venv\Scripts\pip.exe install -r requirements-relay.txt

# Launch Edge with the debug port (Chrome works too)
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222
# In that Edge: open M365 Copilot, start a NEW chat with your MCP agent, copy the URL.

.\.venv\Scripts\python.exe relay\copilot_autopilot_relay.py `
  --conversation-url "https://m365.cloud.microsoft/chat/agent/.../conversation/..." `
  --goal "..." --max-turns 12
```

> Selectors were captured from the live M365 Copilot DOM and isolated in
> `COPILOT_SELECTORS` -- if Microsoft changes the DOM, patch just that block.

---

## 🚀 Setup

### 0. Prereqs

- **Windows 10/11** (PowerShell 5+). Cross-platform mostly, but 🪟-tagged
  tools (PowerShell, processes, registry, scheduler, notifications,
  Outlook, screenshot) are Windows-only.
- **Python 3.10+** (3.11 recommended).
- **Git**.
- Optional, only for the matching tools:
  - **Tesseract OCR** + language data (for `ocr_*`)
  - **Poppler** on PATH (for `ocr_pdf`)
  - **Microsoft PowerPoint** (for `pptx_export_png` COM export)
  - **Microsoft Outlook** (for `outlook_*`)
  - **ODBC Driver 18 for SQL Server** (for `odbc_*`)

### 1. Clone & virtualenv

```powershell
git clone https://github.com/MasayukiTa/m365-copilot-companion-mcp.git
cd m365-copilot-companion-mcp
```

**One-click setup (recommended)** — creates the venv, installs dependencies,
and generates a `.env` with fresh random secrets:

```powershell
.\setup.ps1
# Also install external tools (devtunnel + Tesseract OCR) via winget:
.\setup.ps1 -WithExternalTools
```

Or do it by hand:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Create `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

Generate fresh secrets — **per user, never share, never commit**:

```powershell
python -c "import secrets; print('MCP_API_KEY=' + secrets.token_hex(20))"
python -c "import secrets; print('MCP_UNLOCK_PASSWORD=' + secrets.token_hex(8))"
```

### 3. Start the server

```powershell
.\start.ps1
```

Listens on `http://127.0.0.1:8000/mcp` (Streamable HTTP).

### 4. Expose it (only if a remote client needs it)

Skip for local Claude Desktop. For Copilot Studio, expose `localhost:8000`
over HTTPS via [Dev Tunnels](https://learn.microsoft.com/azure/developer/dev-tunnels/):

```powershell
winget install Microsoft.devtunnel
devtunnel user login                          # first time only
devtunnel create m365-copilot-companion --allow-anonymous
devtunnel port create m365-copilot-companion -p 8000 --protocol http
devtunnel host m365-copilot-companion
```

`--allow-anonymous` is safe because the server enforces a Bearer key and
per-IP unlock on top of the tunnel.

### 5. Register with your MCP client

**Copilot Studio:** Tools → *Add a tool* → *Model Context Protocol*; URL
`https://<your-tunnel>-8000.<region>.devtunnels.ms/mcp`; auth *API key
(manual)*, header `Authorization` = `Bearer <your MCP_API_KEY>`. While
you're there, enable the **native connectors** (mail / calendar / Teams /
SharePoint) you need too — cloud via connectors, local via this companion
(see Design philosophy). Publish to **yourself only** first.

**Claude Desktop / Claude Code:**

```json
{
  "mcpServers": {
    "companion": {
      "transport": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <your MCP_API_KEY>" }
    }
  }
}
```

### 6. Smoke test

Ask the agent to "call `list_my_tools`." 🟢 tools work immediately. 🪟/📦
tools return a "missing dependency" error if their OS/app/library isn't
present (the server itself stays up). The first write/execute prompts you
to `unlock(password="...")`; after that the IP is trusted for
`MCP_UNLOCK_TTL_DAYS` (30).

---

## 🔐 Security model

| Layer | Mechanism | Failure mode |
|---|---|---|
| **Authentication** | Static Bearer token (`MCP_API_KEY`, 40 random hex) | 401 |
| **Filesystem authz** | Every path through `_validate_path`; outside `MCP_ALLOWED_BASE` is rejected | `PermissionError` |
| **Mutation authz** | Write/execute tools call `require_unlocked()`, checking caller IP against a TTL allowlist | "locked" message → `unlock(password)` |

- Unlock password is separate from the API key.
- `127.0.0.1` / `::1` implicitly trusted.
- ODBC opens `readonly=True`, verb-allowlisted; auth inherits the running
  user's existing privileges. No new DB account.
- Rotate the key: edit `.env`, restart.

Not a hardened multi-tenant service, not pen-tested, no protection against
a local attacker who already has your shell, and no protection from you
handing the unlock password to a coworker.

---

## 🛟 The killer feature: self-verification

- `read_image(path)` → base64 data URI a vision model can read.
- `pptx_export_png(pptx_path)` → renders each slide to PNG (needs
  PowerPoint) so the agent can confirm charts embedded and text didn't tofu.

```text
run_python → chart.png   |  read_image → inspect/fix
create_pptx → report.pptx |  pptx_export_png → skim   |  notify_desktop → done
```

Put this loop in the system prompt and "phantom PowerPoint with no charts"
goes away.

---

## 🧩 Suggested system-prompt fragment

```
You operate a companion on the user's PC exposing many MCP tools.

- Call list_my_tools when unsure. If a tool errors with a missing
  dependency, tell the user the prerequisite (OS/app/library) and offer an
  alternative. Not every tool runs in every environment.
- Read-only tools always work. Write/execute need unlock(password) per IP.
- Prefer Copilot Studio's native connectors for cloud work (mail, calendar,
  Teams, SharePoint). The companion's outlook_* etc. are local fallbacks.
- Save outputs under ~/Desktop/<task-name>/.
- After generating an image or deck, self-verify with read_image /
  pptx_export_png; fix and re-export (up to 3 iterations).
- Heavy work: run_python_in_background + job_wait + notify_desktop.
- memory_save durable facts; memory_load before answering known topics.
- Missing a tool? Build it with pip_install + write_file + run_python,
  then tell the user how to add it to TOOLS.
- Never send confidential data to external services (Kroki, web_fetch).
```

---

## 🛠 Troubleshooting

> **🔌 First thing to suspect — if Copilot starts saying it can't respond,
> the MCP server or tunnel has probably dropped.**
> Japanese (confirmed): **「申し訳ございません。それに応答できませんでした。他に何かお手伝いできることはありますか?」**
> English: the equivalent "Sorry, I couldn't respond to that…" message (exact
> wording not yet captured).
>
> When you see it, suspect a dead MCP server / Dev Tunnel before blaming the
> agent. Check, in order:
> 1. Server alive locally: `Test-NetConnection localhost -Port 8000`
> 2. Tunnel actually hosting: `devtunnel show <tunnel> | Select-String "Host connections"`
>    (**0 means dropped** — the process can be alive while the host connection is gone)
> 3. Is the supervisor running? (see below) — if so it self-heals within tens of seconds
> 4. Manual recovery: start the server, then `devtunnel host <tunnel>`

### Keeping it alive (supervisor)

A Dev Tunnel host connection can **silently drop while the process stays
alive** — that's the usual cause of the Copilot error above. The bundled
`supervisor.ps1` polls port 8000 and the tunnel's `Host connections`, and
re-hosts automatically when either is down (with debounce against false
positives and a wait-for-established step so it never kills a host that's
still connecting).

Run it manually:

```powershell
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass `
  -File .\supervisor.ps1 -TunnelName <your-tunnel-name>
```

To **auto-start at every logon** (no admin needed — works even where Task
Scheduler is blocked by org policy), drop a launcher into the **Startup
folder**:

```powershell
$startup = [Environment]::GetFolderPath('Startup')
$root = (Get-Location).Path
@"
@echo off
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "$root\supervisor.ps1" -TunnelName <your-tunnel-name>
"@ | Set-Content -Encoding ASCII (Join-Path $startup "start-companion-supervisor.cmd")
```

It then comes back after reboot / sleep-wake at logon and revives the
server + tunnel (a mutex prevents duplicate instances).

| Symptom | Fix |
|---|---|
| Tool lists but errors on call | Check its 🪟/📦 tag; install the prereq or skip it in this environment |
| `odbc_*` can't connect | Install ODBC Driver 18; verify with `odbc_drivers` |
| `ocr_*` returns nothing | Install Tesseract + language data, or use `read_image` instead |
| `pptx_export_png` fails | Needs Microsoft PowerPoint installed (COM) |
| `outlook_*` fails | Needs Outlook installed; otherwise use the native mail connector |
| `render_diagram` SSL error | Proxy blocking Kroki; fix the CA or draw locally with matplotlib |
| Copilot Studio times out | ~90 s budget; split with `run_in_background` → `job_wait` |
| `unlock` keeps being asked | Caller IP changed; unlock again |

---

## 🤝 Contributing

PRs and issues welcome. Keep docstrings short (the LLM reads them); wrap
mutating tools in `require_unlocked()`; route disk access through
`_validate_path`; tag OS/app/library prerequisites in the docstring and
the README catalog; never commit `.env`, `.unlock_state.json`,
`.memory_state.json`, or business data; document external data egress (☁).

## 📜 License

[MIT](./LICENSE). Use freely, at your own risk, with the warning above in
mind.
