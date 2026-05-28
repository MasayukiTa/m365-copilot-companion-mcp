# m365-copilot-companion-mcp

> Microsoft 365 Copilot に **手** を生やすやつ。
> あなたの貸与ノート PC の上で動く。**100+ ツール**（簡単に数千まで拡張可能）、
> 追加課金ゼロ、構築おおむね 1 人日。

🇯🇵 **このページは日本語版です。English version follows below ↓**

---

## TL;DR

「M365 Copilot は Opus が中にいるのに、ファイルを読むくらいしかしてくれない」
を **物理的に** 解決する個人用 MCP サーバー。あなたの PC で動く Python サーバーが、Copilot に対して

- ファイルを読み・書き・整理する手
- Python を走らせる手（matplotlib でグラフも引ける）
- Excel / CSV / JSON を扱う手
- PowerPoint / Word / PDF を生成・読解する手
- 社内 SQL Server や Azure SQL を Windows 認証で叩く手
- Web を検索・取得する手
- バックグラウンドジョブを管理する手・通知する手・スケジューラに登録する手
- そして **自分の出力を画像として読み返して自己検証する手**

を一式提供する。100+ ツール、追加課金ゼロ、Microsoft 365 Copilot ライセンス内で完結。

```
[ M365 Copilot ]  ──▶  [ Copilot Studio エージェント ]  ──▶  [ Dev Tunnel ]
                                                                  ↓
                                  [ m365-copilot-companion-mcp on あなたのノート PC ]
                                                                  ↓
                                                  ファイル · Python · DB · Web
```

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

## 🎯 何ができるか

`main.py` の `TOOLS` タプルに登録された関数群が、すべて MCP ツールとして公開されます。LLM はツールごとの docstring を読んで、必要に応じて自分で選びます。実行中は `list_my_tools` で全カタログを引けます。

### カテゴリ別の主なツール

| カテゴリ | 主なツール | 何のために |
|---|---|---|
| **コード実行 (Python / shell)** | `run_python`, `shell_exec`, `run_python_in_background`, `run_in_background`, `job_wait`, `job_status`, `job_output`, `job_list`, `job_kill` | コードを走らせる、長いやつは投げて待つ、暴走したら殺す |
| **PowerShell 専用** | `pwsh_exec`, `pwsh_exec_file`, `shell_which` | `cmd.exe` 経由ではなく **PowerShell 5.1 / 7 を直接叩く**（`-NoProfile -NonInteractive -ExecutionPolicy Bypass` 込みなのでスクリプト即動く） |
| **プロセス / サービス / レジストリ** | `process_list`, `process_info`, `process_kill`, `service_status`, `registry_read` | タスクマネージャ + サービスマネージャ + レジストリエディタの読み口（kill は unlock 必須） |
| **ファイル I/O** | `read_file`, `write_file`, `append_file`, `list_directory`, `glob`, `find_files`, `copy_path`, `move_path`, `trash_path`, `create_directory`, `delete_path` | 許可ディレクトリ内のファイルを自在に。`trash_path` はゴミ箱送りで誤操作復元可 |
| **ファイル法医学** | `hash_file`, `find_duplicates`, `dir_size`, `file_metadata` | 「80 GB どこいった？」を 1 プロンプトで解明 |
| **編集・検索** | `grep`, `replace_in_file`, `multi_edit`, `diff_files`, `python_check` | 原子的な複数編集。「ファイル半分食われた」事故が起きない |
| **Git** | `git_status`, `git_diff`, `git_log`, `git_branch`, `git_blame`, `git_add`, `git_commit`, `git_checkout` | 読みも書きも。`git_blame` でツッコむ相手を特定 |
| **表 / JSON** | `read_excel`, `write_excel`, `summarize_table`, `read_json`, `write_json` | Excel / CSV / JSON を一級市民として扱う |
| **PDF / OCR** | `read_pdf`, `pdf_info`, `ocr_image`, `ocr_pdf` | デジタル PDF もスキャン PDF も。OCR は Tesseract 経由 |
| **画像（自己検証）** | `read_image`, `image_info` | エージェントが自分で作った図を **見返して** 確認できる |
| **PowerPoint** | `create_pptx`, `pptx_from_markdown`, `pptx_info`, `pptx_add_slide`, `pptx_add_image`, `pptx_add_table`, `pptx_replace_image`, `pptx_export_png` | スライド生成 → 画像/表埋込 → **各スライドを PNG 化して自己確認** までフル装備 |
| **Word (.docx)** | `create_docx`, `docx_from_markdown`, `docx_info`, `read_docx` | 文書生成と読解。markdown 一発から正式文書まで |
| **Outlook (mail / calendar)** | `outlook_inbox`, `outlook_send_mail`, `outlook_calendar`, `outlook_create_event` | ローカル Outlook を COM で操作。**Graph API 不要、認証は今ログイン中のプロファイルそのまま**。送信系は既定で「下書き保存」 |
| **クリップボード / スクリーン** | `clipboard_get`, `clipboard_set`, `screenshot` | 「いまコピーしたこれ見て」「いま画面に映ってるもの撮って」 |
| **図 / 数式** | `render_diagram` (mermaid / graphviz / plantuml / d2 を [Kroki](https://kroki.io) 経由), `render_mermaid_png`, `render_math` (matplotlib mathtext。LaTeX 不要) | アーキ図と数式を即生成 |
| **Web** | `web_fetch`, `web_search`, `web_search_news`, `github_file` | DuckDuckGo 検索、URL 取得、GitHub raw 取得 |
| **データベース** | `sqlite_*` 6 種、`odbc_*` 6 種 | SQLite と Windows / Entra 統合認証経由の社内 SQL Server / Azure SQL。**read-only 強制**、verb ホワイトリスト |
| **永続記憶** | `memory_save`, `memory_load`, `memory_list`, `memory_delete` | セッション横断のメモ。来週もエージェントが覚えてる |
| **スケジュール / 監視** | `schedule_*` (Windows タスクスケジューラ), `watcher_*` (filesystem watchdog) | 時間軸の自律性。「毎週金曜 9 時に週報生成」 |
| **アーカイブ** | `zip_list`, `zip_extract`, `zip_create` | zip-slip 対策付き |
| **通知** | `notify_desktop` | Windows トースト。`job_wait` と組ませて「集計終わったよ」を能動通知 |
| **環境管理** | `env_info`, `pip_install`, `which`, `list_my_tools` | 実行環境の自己診断、足りないパッケージのその場 install |
| **セキュリティ** | `unlock`, `list_unlocked` | 変更系ツールの IP 単位パスワード解錠 |
| **その他** | `todo_write`, `todo_list`, `todo_clear` | エージェント自身の計画用スクラッチパッド |

### ツールを増やすのは超簡単

`tools/*.py` に Python 関数を 1 つ書き、`main.py` の `TOOLS` タプルに追加して再起動するだけ。docstring が LLM の説明文になる。**現状 100+ ですが、業務ドメイン特化のラッパーを積めば数千ツール規模も普通に届きます**。

---

## 🚀 セットアップ（あなた個人の PC で）

### 0. 前提

- **Windows 10 / 11**（PowerShell 5+）。多くは macOS / Linux でも動きますが `schedule_*` と `notify_desktop` は Windows 専用
- **Python 3.10 以降**（3.11 推奨）
- **Git**
- 任意:
  - **Tesseract OCR** + 言語データ（日本語 OCR なら `jpn.traineddata`、[UB-Mannheim ビルド](https://github.com/UB-Mannheim/tesseract/wiki) が手軽）
  - **Poppler** が PATH 上にあると `ocr_pdf` が動く
  - **ODBC Driver 18 for SQL Server**（社内 SQL Server / Azure SQL に繋ぐなら）

### 1. クローン & 仮想環境

```powershell
git clone https://github.com/MasayukiTa/m365-copilot-companion-mcp.git
cd m365-copilot-companion-mcp

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
5. 保存 → 「接続を追加」 → 100+ ツールが見えれば成功

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

100+ ツールが返れば配線 OK。読み取り系（`read_file`, `list_directory` 等）は即使えます。書込・実行系を初めて使うと:

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
- `pptx_export_png(pptx_path)`: PowerPoint を COM 経由で起動し、各スライドを PNG にエクスポートする。`create_pptx` の後にこれを呼んで、エージェント自身が「画像が入っているか」「日本語が豆腐化していないか」を確認する

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
│   ├── jobs.py              # 非同期ジョブ管理
│   ├── file_ops.py          # ファイル I/O + 法医学
│   ├── search_ops.py        # glob, find_files
│   ├── archive_ops.py       # zip 操作
│   ├── coding_ops.py        # grep, multi_edit, git_*, diff_files, python_check
│   ├── data_ops.py          # Excel / CSV / JSON
│   ├── pdf_ops.py           # PDF テキスト抽出
│   ├── ocr_ops.py           # Tesseract ラッパー
│   ├── image_ops.py         # read_image, image_info（自己検証）
│   ├── pptx_ops.py          # PowerPoint 生成 + COM エクスポート
│   ├── docx_ops.py          # Word (.docx) 生成・読解
│   ├── outlook_ops.py       # Outlook COM (mail / calendar)
│   ├── clipboard_ops.py     # クリップボード読み書き
│   ├── screenshot_ops.py    # スクリーンキャプチャ
│   ├── diagram_ops.py       # Kroki ベースの図生成
│   ├── render_ops.py        # matplotlib mathtext の数式レンダラ
│   ├── web_ops.py           # web_fetch, github_file
│   ├── search_web.py        # DuckDuckGo 検索
│   ├── sql_ops.py           # SQLite（read-only）
│   ├── odbc_ops.py          # ODBC / SQL Server / Azure SQL
│   ├── schedule_ops.py      # Windows タスクスケジューラのラッパー
│   ├── watcher_ops.py       # フォルダ監視（watchdog）
│   ├── memory_ops.py        # クロスセッション記憶
│   ├── notify_ops.py        # Windows トースト
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

- 何が使えるか不明な時は最初に list_my_tools を呼ぶ。
- 読取系は常に使える。書込・実行系は IP 単位で unlock(password) を必要とする。
  ロック解除を求められたらユーザーに 1 度だけ伝えればよい。
- 出力ファイルは原則 ~/Desktop/<案件名>/ 配下に保存する。
- 画像や PowerPoint を生成した直後は read_image / pptx_export_png で自己検証し、
  画像欠落・豆腐化・レイアウト崩れがあれば最大 3 回まで修正と再エクスポートを
  ループする。
- 重い処理は run_python_in_background + job_wait で投げ、終わったら
  notify_desktop でユーザーに能動通知する。ユーザーをスピナーで待たせない。
- ユーザーや案件について長期的に役立つ情報を知ったら memory_save する。
  既出の話題には memory_load / memory_list で取りに行ってから回答する。
- 機密データを外部サービス（Kroki, web_fetch 等）に流さない。迷ったらローカル処理。
```

---

## 🛠 トラブルシュート

| 症状 | 対処 |
|---|---|
| `pyodbc` 接続不可 | ODBC Driver 18 for SQL Server をインストール、`odbc_drivers` で確認 |
| OCR が空を返す | Tesseract と言語データ（`jpn.traineddata` 等）を入れて `which("tesseract")` で確認 |
| `pptx_export_png` 失敗 | ホスト PC に Microsoft PowerPoint がインストールされている必要あり（COM 経由） |
| `render_diagram` で SSL エラー | 社内プロキシが Kroki をブロック。CA bundle を直すか、本ツールを使わず matplotlib でローカル描画させる |
| Copilot Studio がタイムアウト | 1 リクエストの実質予算は ~90 秒。`run_in_background` → `job_wait` で分割 |
| `unlock` を何度も要求される | 呼び出し元 IP が変わった（VPN 切替、Copilot Studio バックエンドの hop）。再 unlock |

---

## 🇯🇵 よくある質問（日本企業で運用する人向け）

**Q. 情シスに何と説明すれば？**
A. 「自分の貸与 PC 上で動く個人ツールで、外部にデータは送らず、自分が既にアクセス権を持つリソースにしかアクセスしません」が正確。`MCP_ALLOWED_BASE` で許可パスを絞っていること、ODBC は read-only で Windows 認証を継承していることを併せて伝えると話が早い。それでも NG なら、潔く諦めましょう。

**Q. 上司に「これ何？」と聞かれた**
A. 冒頭の図を見せて「Copilot に手を生やすやつ」で多くは伝わる。技術系の上司なら「Dev Tunnel で localhost に MCP」まで言えば終わり。事務系の上司なら「Excel の集計を Copilot に頼んだら本当にやってくれるようになる」が刺さる。

**Q. 監査でバレないか？**
A. **バレます**。隠す前提で運用しないでください。Dev Tunnel のログは Microsoft に残り、Copilot Studio の操作履歴も残ります。最初から「会社の AI 活用の試行として、自分の PC で動かしている」と説明できる形で運用してください。

**Q. 同僚にも使わせたい**
A. それは「共有」「公開」になります。Copilot Studio のチャネル設定で「自分のみ」を外す前に、情シスとの会話を 1 度挟んでください。共有用途は本リポの想定範囲外です。

**Q. データが Microsoft に流れない？**
A. M365 Copilot のチャット内容と、エージェントが返した文字列は Microsoft の Copilot Studio に流れます。これはエージェントを Copilot Studio で動かす以上、避けようがありません。ローカルファイルの中身も、エージェントが回答に含めればそのままチャットに出ます。**取り扱い注意の情報は最初からエージェントに見せないこと**。`MCP_ALLOWED_BASE` を限定するのは、この事故を物理的に減らすための仕切りです。

**Q. Cowork（社内で導入された M365 系・Claude 系エージェントの総称）が会社で禁止になった。これは大丈夫？**
A. 別物ですが、判断は組織の方針次第です。本ツールは「Microsoft が公式に提供するチャネル（M365 Copilot）に MCP として接続するためのサーバー」であり、Anthropic の API を直接利用するものではありません。Cowork 禁止の理由が「Anthropic 直接利用の禁止」であれば本ツールは抵触しません。「ローカル AI エージェント全般の禁止」「個人で立てる MCP サーバーの禁止」であれば抵触します。**ご自身の組織の禁止理由を確認し、必要なら情シスに事前承認を取ったうえで判断してください**。同じ呼称でも各社の運用ルールは異なります。

**Q. 退職時はどうする？**
A. `.env` の API キーと unlock パスワードは破棄。Dev Tunnel と Copilot Studio エージェントは削除。`.unlock_state.json` `.memory_state.json` `.todo_state.json` `agent_memory/` 配下も削除。PC 返却時にこの掃除は必須です。

---

## 🤝 コントリビュート

PR や Issue 歓迎。お願い:

- ツールの docstring は短く具体的に。**LLM が読みます**、人間だけが読むわけではありません
- 変更系ツールは必ず `tools/security.py` の `require_unlocked()` をラップ
- ディスクアクセスは必ず `_validate_path` を通して `MCP_ALLOWED_BASE` を越えないように
- **絶対にコミットしないもの**: `.env`, `.unlock_state.json`, `.memory_state.json`, 業務データ。`.gitignore` で塞いであります、緩めないでください
- 新ツールが外部サービスを呼ぶ場合、何のデータがどこに出ていくか docstring に明記。運用者がオプトアウトできるように

## 📜 ライセンス

[MIT](./LICENSE)。自己責任で自由にどうぞ。上の警告を頭に置いたうえで。

---
---

# 🇺🇸 English version

> Microsoft 365 Copilot shipped you a brain. It forgot the hands.
> This is the hands.

A personal-use **Model Context Protocol (MCP) server** that turns one
laptop into a fully-capable agent backend for **Microsoft 365 Copilot**,
**Claude Desktop**, or any other MCP-aware client. **100+ tools** (and
trivially extensible to thousands). Zero external API keys. Built in
roughly a day. Used in anger ever since.

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
                                              your files · Python · DBs · web
```

One user, one companion, one laptop. Nothing is centralised. Nothing
leaves the box you wouldn't want it to. The whole thing costs **zero**
beyond the M365 Copilot licence you already have.

> Not affiliated with, endorsed by, or sponsored by Microsoft Corporation.
> "Microsoft 365", "Copilot", and "Copilot Studio" are trademarks of their
> respective owners; they are referenced here only to describe what this
> companion attaches to.

---

## ⚠️ Before you go any further

This is a thing you run **on a machine you control**, against accounts
and data **you already have permission to touch**, for **your own use**.
It is not a SaaS, it is not a hosted service, there is no support
contract, and the friendly Microsoft-flavoured name in the repo does
not make any of that less true.

If your employer:

- blocks personal MCP servers,
- has not approved Microsoft Dev Tunnels for use,
- has a policy against running Copilot Studio agents outside an
  IT-managed template,
- forbids installing arbitrary Python packages on company hardware,
- considers any of: AI tool execution, agent file access, or
  third-party GitHub clones a security incident,

…then **do not deploy this on a company laptop**. Read it for ideas,
build your own equivalent through proper channels, but don't paste the
Bearer key into your work Copilot Studio and hope nobody notices.

The licence is MIT. There is no warranty, no obligation, no liability.
**Whatever you blow up on your side is your problem.** Move carefully,
especially with the unlock password.

OK. With that out of the way:

---

## 🎯 What this thing can do

`main.py` registers a single flat catalog of tools. Call `list_my_tools`
at runtime to see all of them. Currently **100+ and growing**; new tools
are about 30 lines of Python plus an entry in the `TOOLS` tuple.

| Category | Tools | What it's for |
|---|---|---|
| **Code execution (Python / shell)** | `run_python`, `shell_exec`, `run_python_in_background`, `run_in_background`, `job_wait`, `job_status`, `job_output`, `job_list`, `job_kill` | Run code. Wait for results. Kill the runaway. |
| **PowerShell** | `pwsh_exec`, `pwsh_exec_file`, `shell_which` | Dedicated PowerShell entry points with `-NoProfile -NonInteractive -ExecutionPolicy Bypass` defaults. Works against Windows PowerShell 5.1 or PowerShell 7 if installed. |
| **Processes / services / registry** | `process_list`, `process_info`, `process_kill`, `service_status`, `registry_read` | Task Manager + Services + Registry Editor as MCP tools (kill requires unlock). |
| **File I/O** | `read_file`, `write_file`, `append_file`, `list_directory`, `glob`, `find_files`, `copy_path`, `move_path`, `trash_path`, `create_directory`, `delete_path` | Read, write, move, delete inside the allowed base. |
| **File forensics** | `hash_file`, `find_duplicates`, `dir_size`, `file_metadata` | "Where did 80 GB go?" — answered in one prompt. |
| **Editing / search** | `grep`, `replace_in_file`, `multi_edit`, `diff_files`, `python_check` | Atomic multi-edit. No more "the agent ate half my file." |
| **Git** | `git_status`, `git_diff`, `git_log`, `git_branch`, `git_blame`, `git_add`, `git_commit`, `git_checkout` | Reads and writes. `git_blame` finds the colleague to gently roast. |
| **Tabular / JSON** | `read_excel`, `write_excel`, `summarize_table`, `read_json`, `write_json` | First-class spreadsheet handling. |
| **PDF / OCR** | `read_pdf`, `pdf_info`, `ocr_image`, `ocr_pdf` | Digital and scanned. |
| **Image (self-verify)** | `read_image`, `image_info` | The agent can finally see what it just made. |
| **PowerPoint** | `create_pptx`, `pptx_from_markdown`, `pptx_info`, `pptx_add_slide`, `pptx_add_image`, `pptx_add_table`, `pptx_replace_image`, `pptx_export_png` | Generate decks, embed real charts, then render each slide back to PNG to audit. |
| **Word (.docx)** | `create_docx`, `docx_from_markdown`, `docx_info`, `read_docx` | Author and read Word documents. Markdown in, .docx out. |
| **Outlook (mail / calendar)** | `outlook_inbox`, `outlook_send_mail`, `outlook_calendar`, `outlook_create_event` | Drive the locally installed Outlook over COM. **No Graph API required** — inherits the signed-in user's mailbox and calendar. Sends save to Drafts by default. |
| **Clipboard / screen capture** | `clipboard_get`, `clipboard_set`, `screenshot` | "Look at what I just copied" / "Take a snap of my screen and check what's open." |
| **Diagrams / math** | `render_diagram` (mermaid / graphviz / plantuml / d2 via [Kroki](https://kroki.io)), `render_mermaid_png`, `render_math` (matplotlib mathtext, no LaTeX install needed) | Architecture diagrams and equations on demand. |
| **Web** | `web_fetch`, `web_search`, `web_search_news`, `github_file` | DuckDuckGo search, URL fetching, raw GitHub file pulls. |
| **Databases** | `sqlite_*` and `odbc_*` (six each) | Read-only SQL over Windows / Entra integrated auth. No DBA involvement required. |
| **Persistent memory** | `memory_save`, `memory_load`, `memory_list`, `memory_delete` | Cross-session notes the agent remembers next week. |
| **Scheduling / watching** | `schedule_*` (Windows Task Scheduler), `watcher_*` (filesystem watchdog) | Time-axis autonomy. "Every Friday at 9, regenerate the weekly report." |
| **Archives** | `zip_list`, `zip_extract`, `zip_create` | With zip-slip protection. |
| **Notifications** | `notify_desktop` | Windows toast. Pair with `job_wait` for "ping me when the model run finishes." |
| **Environment** | `env_info`, `pip_install`, `which`, `list_my_tools` | Introspect the host; install missing packages on the fly. |
| **Security** | `unlock`, `list_unlocked` | Per-IP password unlock for mutating tools. |
| **Misc** | `todo_write`, `todo_list`, `todo_clear` | A scratchpad for the agent's own planning. |

---

## 🚀 Setup

### 0. Prereqs

- **Windows 10 or 11** (PowerShell 5+). Most things work cross-platform;
  `schedule_*` and `notify_desktop` are Windows-only.
- **Python 3.10+** (3.11 recommended).
- **Git**.
- Optional but useful:
  - **Tesseract OCR** + the language pack you need
    ([UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki))
  - **Poppler** on PATH if you want `ocr_pdf`
  - **ODBC Driver 18 for SQL Server** if you'll query corporate DBs over
    Entra

### 1. Clone & virtualenv

```powershell
git clone https://github.com/MasayukiTa/m365-copilot-companion-mcp.git
cd m365-copilot-companion-mcp

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

Paste into `.env`. Tighten `MCP_ALLOWED_BASE` to a sub-folder if you
want the companion to live in a smaller pen.

### 3. Start the server

```powershell
.\start.ps1
```

The MCP server listens on `http://127.0.0.1:8000/mcp` (Streamable HTTP).

### 4. Expose it (only if a remote client needs to reach it)

For local Claude Desktop on the same laptop, skip this step entirely.

For Microsoft 365 Copilot Studio you need an HTTPS URL pointing at
`localhost:8000`. The simplest free option is
[Microsoft Dev Tunnels](https://learn.microsoft.com/azure/developer/dev-tunnels/):

```powershell
winget install Microsoft.devtunnel
devtunnel user login                          # first time only
devtunnel create m365-copilot-companion --allow-anonymous
devtunnel port create m365-copilot-companion -p 8000 --protocol http
devtunnel host m365-copilot-companion
# → https://<random>-8000.<region>.devtunnels.ms
```

`--allow-anonymous` is safe **because** this server enforces a Bearer
API key and a per-IP unlock layer on top of the tunnel.

### 5. Register with your MCP client

**Microsoft 365 Copilot Studio:**

1. Create / open your Copilot Studio agent.
2. Tools → *Add a tool* → *Model Context Protocol*.
3. Server URL: `https://<your-tunnel>-8000.<region>.devtunnels.ms/mcp`.
4. Authentication: *API key (manual)*. Header name `Authorization`,
   value `Bearer <your MCP_API_KEY>`.
5. Save → *Add connection* → roughly 100 tools should appear.

Publish via the "Microsoft 365 + Teams" channel, scoped to **yourself
only**, until you have made peace with what the agent can now reach.

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

Ask the agent:

> "Call `list_my_tools`."

You should see roughly 100. Read-only tools just work. The first time
you try something that writes or executes, you'll be told:

> `[locked client IP: '203.0.113.42'] Call unlock(password='...') first.`

Do as you're told. The IP is then trusted for `MCP_UNLOCK_TTL_DAYS`
(30 by default). Subsequent mutations are silent.

---

## 🔐 Security model

Three layers stacked in order:

| Layer | Mechanism | Failure mode |
|---|---|---|
| **Authentication** | Static Bearer token (`MCP_API_KEY`, 40 random hex chars) | 401 Unauthorized |
| **Filesystem authorisation** | Every path goes through `_validate_path`, which rejects anything outside `MCP_ALLOWED_BASE` | `PermissionError` |
| **Mutation authorisation** | Write / execute tools call `require_unlocked()`, which checks the caller IP (from `X-Forwarded-For` or socket peer) against an unlock list with TTL | Returns a polite "locked" message instructing the caller to run `unlock(password)` |

Worth knowing:

- The unlock password is **separate** from the API key. Either alone is
  not enough to mutate.
- `127.0.0.1` / `::1` are implicitly trusted (local dev).
- ODBC queries are opened `readonly=True` and verb-allowlisted to
  `SELECT / WITH / EXEC / SHOW / DESCRIBE`. Auth is whatever Windows or
  Entra hands the running user. No new DB account, no new password.
- Rotate the Bearer key by editing `.env` and restarting. Old keys die
  immediately.

What this is **not**:

- It is not a hardened multi-tenant service.
- It is not pen-tested.
- It does not protect you from a local attacker who has shell on your
  laptop — that attacker already has everything the agent has.
- It does not protect your organisation from you handing the unlock
  password to a coworker. Don't do that.

---

## 🛟 The killer feature: self-verification

Most agent failures happen because the model claims a task is done but
never actually checks. This server gives the agent two tools to inspect
its own output:

- `read_image(path)` returns a base64 data URI consumable by
  vision-capable models. Generate a chart with `run_python` (matplotlib),
  then ask the agent to read it back and confirm the axes are labelled.
- `pptx_export_png(pptx_path)` drives PowerPoint over COM to render
  every slide to PNG. Use this after `create_pptx` to confirm images
  embedded correctly and Japanese characters didn't tofu.

A typical "trust but verify" loop:

```text
run_python       →  saves chart.png
read_image       →  agent inspects, fixes mistakes
create_pptx      →  embeds chart.png into report.pptx
pptx_export_png  →  agent skims each slide PNG
notify_desktop   →  "Report ready: report.pptx"
```

Wire this loop into the system prompt of your Copilot Studio agent and
the "phantom PowerPoint with no charts" failure mode disappears.

---

## 🧩 Suggested system-prompt fragment

Paste into your Copilot Studio (or Claude Desktop) agent instructions:

```
You are the operator of the companion. The companion lives on the user's
PC and exposes a wide set of MCP tools.

- When unsure what is available, call list_my_tools first.
- Read-only tools are always available. Mutating / executing tools
  require unlock(password) per IP; surface the error to the user once
  if it happens.
- Save outputs under ~/Desktop/<task-name>/ by default.
- Right after generating an image or slide deck, call read_image or
  pptx_export_png and self-verify. If something is missing or tofu'd,
  fix it and re-export — silently is fine, up to three iterations.
- For heavy or slow work, prefer run_python_in_background + job_wait
  and notify_desktop when done. Don't make the user stare at a spinner.
- When you learn something durable about the user or a project,
  memory_save it. Read with memory_load / memory_list before answering
  about previously discussed topics.
- Never send confidential data to external services (Kroki, web_fetch, etc.).
  When in doubt, do it locally.
```

---

## 🛠 Troubleshooting

| Symptom | Fix |
|---|---|
| `pyodbc` can't connect | Install ODBC Driver 18 for SQL Server; verify with `odbc_drivers` |
| OCR returns nothing | Install Tesseract + the language data, then run `which("tesseract")` |
| `pptx_export_png` fails | Microsoft PowerPoint must be installed on the host (it drives PowerPoint over COM) |
| `render_diagram` SSL errors | Corporate proxy blocking Kroki. Either get the CA right or skip the tool and let the agent draw locally with matplotlib |
| Copilot Studio request times out | Each Copilot Studio call has ~90 s budget. Split with `run_in_background` → `job_wait` |
| `unlock` keeps being requested | Caller IP changed (VPN switch, Copilot Studio backend hop). Unlock again. |

---

## 🤝 Contributing

PRs and issues welcome. Please:

- Keep tool docstrings short and concrete — the LLM reads them, not
  just humans.
- Wrap any mutating tool in `require_unlocked()` from
  `tools/security.py`.
- Route any disk access through `_validate_path` so it stays inside
  `MCP_ALLOWED_BASE`.
- **Never** commit `.env`, `.unlock_state.json`, `.memory_state.json`,
  or business data. The `.gitignore` is set up for this; please don't
  loosen it.
- If a new tool calls an external service, document what data leaves
  the device and let the operator opt out.

## 📜 License

[MIT](./LICENSE). Use freely, at your own risk, with the warning above
firmly in mind.
