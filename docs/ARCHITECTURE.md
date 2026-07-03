# アーキテクチャ

このドキュメントは m365-copilot-companion-mcp の内部構造（コンポーネント・起動フロー・ポート）をまとめます。全体像は [README](../README.md) を先に読んでください。

---

## 役割分担 — どこまでがこの companion の仕事か

これを最初に頭に入れると全部が分かります。

```
┌─────────────────────────────────────────────────────────────┐
│  M365 クラウド側                                              │
│  メール送受信 / 予定表 / Teams 投稿 / SharePoint 検索 / Web 検索 │
│      ↑ Copilot Studio が公式に用意する「純正コネクタ」を        │
│        自分でオンにするのが本筋。認証も監査も Microsoft 管理。   │
├─────────────────────────────────────────────────────────────┤
│  あなたのローカル PC 側                                        │
│  ファイル / Python 実行 / ローカル・社内 DB / Office 生成 / シェル │
│      ↑ Copilot Studio のコネクタでは届かない領域。             │
│        この MCP サーバーが担当するのは ここ。                   │
└─────────────────────────────────────────────────────────────┘
```

本来の使い方は **「Copilot Studio の純正コネクタ（クラウド側）」＋「この companion（ローカル側）」の二枚重ね** です。メール・カレンダー・SharePoint をエージェントに触らせたいなら、Copilot Studio 側でその純正コネクタをオンにするのが正解。Microsoft が認証・監査を持つので社内承認も通しやすい。

本リポに `outlook_*`（メール/カレンダー）や `web_search` があるのは、**純正コネクタを設定できない／したくない環境向けのローカル退避路** だからです。`outlook_*` は Graph API 登録なしに、いま PC にログイン中の Outlook を COM 経由で借ります。`web_search` も Copilot 標準の Web 検索が使えるなら不要です。

迷ったら原則: **クラウドで完結する仕事は純正コネクタ、ローカル PC を触る仕事はこの companion。**

---

## データの流れ

```
[ M365 Copilot ] ──▶ [ Copilot Studio エージェント ] ──▶ [ devtunnel ]
                                                              ↓
                            [ MCP サーバー(main.py) on あなたのPC ]
                                                              ↓
                          ファイル · Python · DB · Office生成 · OCR …
```

relay / fleet はこの経路とは別に、Edge 上の Copilot Web UI を CDP で駆動して会話を無人で回します。

---

## コンポーネント

### MCP サーバー本体（`main.py`）

FastMCP のエントリポイント。`tools/*.py` の関数を `TOOLS` タプルに登録し、MCP ツールとして公開します。LLM はツールごとの docstring を読んで自分で選びます。`http://127.0.0.1:8000/mcp`（Streamable HTTP）で待受します。

既定の **map mode**（`MCP_TOOL_MAP=1`）では、最小コア＋`call_tool` ゲートウェイのみを MCP に登録し、残りのツールはゲートウェイ経由で呼びます。これは Copilot Studio の 70 ツール上限とトークン枠を回避するためです（詳細は [CONFIG.md](CONFIG.md)）。

`main.py` には執筆時点で 138 個のツールが収録されています。実行中に有効なツールは `list_my_tools` で確認できます。

### relay（自動中継器・`relay/copilot_autopilot_relay.py`）

ゴールを 1 個渡すと、Edge 上の Copilot 会話を **CDP 経由で完了まで自律駆動** するスタンドアロンのコントローラです。OS のマウス・キーボードフォーカスを奪わないので、裏で回っている間もあなたは別ウィンドウで作業できます。唯一の知能は Copilot エージェント自身で、relay 側は「完了を検知して次の job を投げる」決定的な配管だけ（生成 AI を使わない＝完全無人）。

- 各ターンをクロスセッション memory と監査ランログ（operator D）に保存
- `stop_request()`（kill-switch）を毎ターン＆長時間待機中もポーリング
- DONE / STUCK / MAXTURNS / ABORTED のいずれでも `notify_desktop` で通知
- セレクタはライブ DOM から採取し `COPILOT_SELECTORS` に隔離（Microsoft が DOM を変えたらそこだけ直す）
- 制御ループは `relay/test_relay_loop.py` が全終了パス（7 シナリオ）をブラウザ無しで検証

関連: `relay/code_task.py`（自然言語 1 行のコーディングフロントドア）・`relay/acceptance.py`（検証ゲート）・`relay/project_introspect.py`（検証コマンド自動判定）・`relay/repo_map.py`（リポジトリ地図）・`relay/planner.py`（プラン提示→承認）。詳細は [ADVANCED.md](ADVANCED.md)。

### fleet（並列フリート・`relay/fleet_runner.py`）

複数ゴールを 1 スレッドのノンブロッキング・ラウンドロビンで並走させます。各ゴールが自分の Copilot 会話を持ち、終了した瞬間にタブを閉じて（RAM を解放して）次のゴールを開きます。RAM 連動 autoscale・watchdog による再接続・再開などを持ちます（[ADVANCED.md](ADVANCED.md) 参照）。並列 UI は `ui/FleetCockpit.cs`。

### bridge（チャット UI・`bridge/copilot_bridge.py`）

Premium / Direct Line を使わず、stdlib の `http.server` だけで自己完結の HTML チャットを配信し、Copilot の応答を差分スクレイピングでトークン単位ストリーミングします。ブラウザで `http://127.0.0.1:8765` にアクセスして使います。bridge は専用プロファイル `copilot-bridge-edge`（CDP `:9223`）で立つので、fleet の Edge（`:9222`）と取り合わず同時に使えます。

ネイティブ WPF チャット `ui/CopilotChat.cs` も裏は同じ経路です。

### supervisor（常時公開・`scripts/supervisor.ps1`）

devtunnel の host 接続は、プロセスが生きていても relay 接続だけが静かに落ちることがあります。supervisor はポート 8000 とトンネルの `Host connections` を定期監視し、落ちていれば自動で張り直します（デバウンス＋接続確立待ち付き）。ログオン時自動起動の登録方法は [ADVANCED.md](ADVANCED.md)。

### 専用 companion Edge（`scripts/start_companion_edge.ps1`）

`copilot-companion-edge` という専用プロファイル（`%LOCALAPPDATA%\copilot-companion-edge`）で Edge を起動し、CDP を `:9222` でバインドします。普段使いの Edge とは完全分離するので、本体 Edge に M365 タブを何枚開いても RAM を奪い合わず、本体クラッシュにも巻き込まれません。ウィンドウモード（`-Foreground` / `-Headless` / `-Background` / `-Surface` / `-HardReset`）の詳細は [ADVANCED.md](ADVANCED.md)。

---

## 起動フロー

- **初回**: `quickstart.bat` が venv 作成・依存導入・`.env` 生成・devtunnel 設定・Copilot Studio 手作業のガイド・スタック起動まで通します（[README のセットアップ](../README.md#セットアップ)）。
- **毎日**: デスクトップの「M365 Companion」（= `start_all.bat`）をダブルクリック。supervisor（サーバー＋トンネル host）→ 専用 Edge `:9222` → bridge `:9223` → CopilotChat / FleetCockpit を冪等に一括起動します。既に動いているものは触りません。
- **個別起動**: `.\scripts\supervisor.ps1` / `.\scripts\start_companion_edge.ps1 -Headless` / `.\scripts\start_bridge.ps1 -Keepalive` / `.\ui\rebuild_ui.ps1`。

---

## ポート一覧

| ポート | 用途 |
|---|---|
| `8000` | MCP サーバー本体（`main.py`。devtunnel が公開する） |
| `8765` | bridge のチャット UI（`MCP_BRIDGE_PORT`） |
| `9222` | 専用 companion Edge の CDP（fleet / エージェント用。`MCP_CDP_PORT`） |
| `9223` | 専用 bridge Edge の CDP（bridge 用・fleet と分離） |
| `8011` | OpenAI 互換エンドポイント（ベンチ・任意。[ADVANCED.md](ADVANCED.md)） |

---

## リポジトリ構成

```
m365-copilot-companion-mcp/
├── main.py                  # FastMCP のエントリポイント、ツール登録
├── quickstart.bat           # 初回セットアップ（ダブルクリック）
├── start_all.bat            # 毎日の起動（ダブルクリック）
├── doctor.bat               # 健康診断
├── configure_env.bat        # エージェント URL 設定ダイアログ
├── copilot_studio_values.bat# Copilot Studio に貼る値を表示
├── rotate_secrets.bat       # 秘密の再発行
├── setup.bat                # Python 環境ブートストラップ
├── requirements.txt / requirements-relay.txt
├── .env.example             # コピーして .env を作る
├── LICENSE                  # MIT
│
├── scripts/                 # .bat から呼ばれる実装スクリプト
│   ├── start.ps1            # 起動（.venv 自動検出）
│   ├── start_all.ps1        # スタック一括起動
│   ├── supervisor.ps1       # サーバー＋トンネル監視・自動復旧
│   ├── setup_devtunnel.ps1  # devtunnel 導入・サインイン・作成
│   ├── start_companion_edge.ps1 / start_bridge.ps1   # 専用 Edge 起動
│   ├── configure_env.ps1 / copilot_studio_values.ps1 / doctor.ps1
│   ├── rotate_secrets.py / bootstrap.py
│   └── win/                 # 内部専用の小物（edge_keeper.ps1 等）
│
├── tools/                   # ツール実装（1 ファイル 1 カテゴリ）
│   ├── code_exec.py         # run_python, shell_exec
│   ├── file_ops.py          # ファイル I/O + ディスク調査
│   ├── coding_ops.py        # grep, multi_edit, git_*, diff_files
│   ├── data_ops.py          # Excel / CSV / JSON
│   ├── pdf_ops.py / ocr_ops.py / image_ops.py
│   ├── pptx_ops.py / docx_ops.py
│   ├── outlook_ops.py       # 🪟 Outlook COM（ローカル退避路）
│   ├── sql_ops.py / odbc_ops.py   # SQLite / 社内 DB
│   ├── schedule_ops.py / watcher_ops.py / notify_ops.py
│   ├── memory_ops.py / task_ops.py / env_ops.py
│   ├── registry.py          # @register と list_my_tools
│   └── security.py          # unlock / require_unlocked / IP ホワイトリスト
│
├── relay/                   # relay / fleet / 検証ゲート
├── bridge/                  # Python チャットブリッジ
├── ui/                      # WPF アプリ（CopilotChat / FleetCockpit）
├── bench/                   # HumanEval / SWE-bench / GAIA ハーネス
└── agent_memory/            # 長期メモ（大半は git 除外）
```

---

## 推奨システムプロンプト断片

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

← [README に戻る](../README.md)
