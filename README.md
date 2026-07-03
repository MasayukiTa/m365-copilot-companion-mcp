# m365-copilot-companion-mcp

M365 Copilot を、**あなたの PC を操作できる自律エージェント**にするツールです。
API 契約は不要。会社アカウントのまま。管理者権限も不要。
ファイルを読むだけだった Copilot に「手」を生やして、実際に作業させます。

> **English TL;DR** — This turns Microsoft 365 Copilot into an autonomous agent that operates your own PC — files, Excel, OCR, Python, local/corporate databases — with no API contract, on your normal work account, without admin rights. A small Python MCP server on your laptop exposes the tools; the relay drives the Copilot web UI unattended. **To install: double-click `quickstart.bat` and follow the prompts.** The one manual step (registering the MCP tool in Copilot Studio) is walked through with screenshots below.

---

## これは何

- M365 Copilot は中身が賢いのに、チャットに貼った文章を読むくらいしかしてくれません。
- このツールは、あなたのノート PC で動く小さなサーバーを Copilot に繋ぎ、**ファイル操作・Python 実行・Excel・OCR・社内 DB といった「手」**を与えます。
- 追加課金ゼロ。いま持っている M365 Copilot ライセンスの中だけで完結します。

---

## できること

- **ファイル**を読む・書く・整理する・重複を探す
- **Excel / CSV / JSON** を集計する、**Word / PowerPoint / PDF** を作る・読む
- **OCR** で画像・スキャン PDF を文字起こしする
- **Python** をその場で走らせてグラフを描く
- **社内 SQL(ODBC)** を Windows 認証のまま読み取る（read-only）
- 複数の作業を**並列で無人自走**させる（フリート）
- **チャット UI** で手元のアプリのように話しかける
- 自分が作った画像を**見返して自己検証**する

---

## 必要なもの

1. **Windows 10 / 11**
2. **M365 Copilot ライセンス**（職場アカウント）
3. **Copilot Studio を開けること**（`https://copilotstudio.microsoft.com`）

---

## セットアップ

### 1. 入手する

git を使うなら:

```powershell
git clone https://github.com/MasayukiTa/m365-copilot-companion-mcp.git
```

git を使わないなら: GitHub ページの緑色の「**Code**」ボタン →「**Download ZIP**」→ ダウンロードした zip を右クリック →「**すべて展開**」。

### 2. `quickstart.bat` をダブルクリックする

展開したフォルダ直下の **`quickstart.bat`** をダブルクリックします。あとは画面の指示に従うだけです。

> **英語の質問が出たら、そのまま `Enter` を押せば安全な既定が選ばれます。** 迷ったら Enter で問題ありません。

### 3. 画面の指示に従う

`quickstart.bat` は 7 ステップを順にガイドします。**手作業は STEP 5 の Copilot Studio だけ**で、あとはクリックとコピペです。

| STEP | 何が起きる | あなたがやること |
|---|---|---|
| 1 | Python 環境を自動構築（管理者不要） | 待つ |
| 2 | Bearer トークン / アンロックパスワードを表示（`.env` 自動生成） | **メモする** |
| 3 | git 更新確認（ZIP 配布なら自動スキップ） | 待つ |
| 4 | devtunnel を導入・サインイン・作成し、公開 URL を表示 | サインインをポチポチ |
| 5 | **一時停止** → Copilot Studio で MCP を登録（唯一の手作業） | 下の walkthrough 参照 |
| 6 | エージェント URL ダイアログが自動で開く | URL をコピペ |
| 7 | スタック全部を起動（サーバー＋トンネル＋Edge＋UI） | 待つ |

全部 1 つの黒い窓＋ダイアログ＋サインイン画面で完結します。別 PC・別ターミナルは要りません。

---

### STEP 5 の手作業 — Copilot Studio に MCP を登録する

これが唯一の手作業です。実際の画面つきで順に進みます。アクセス先は `https://copilotstudio.microsoft.com`。

**1. ホームの「ゼロから構築を開始する」→「エージェント」タイルをクリック**

![ホームの「ゼロから構築を開始する」→「エージェント」タイル](docs/images/cs_01_home_agent_tile.png)

**2. エージェントに名前を入力 →「作成」**

名前（例: `companion`）を入力し「**作成**」をクリックします。

![「エージェントに名前をつける」ダイアログで名前を入れて「作成」](docs/images/cs_02_name_agent.png)

**3. 編集画面の「ツール」セクション →「+ ツールを追加する」**

![エージェント編集画面の「ツール」セクションの「+ ツールを追加する」](docs/images/cs_03_tools_section.png)

**4. 「ツールを追加する」ダイアログで「新規追加 MCP」タイルを選ぶ**

見当たらなければダイアログ下部の「**表示を増やす**」を押すと出ます。

![「ツールを追加する」ダイアログの「新規追加 MCP」タイル](docs/images/cs_04_add_tool_mcp_tile.png)

<details>
<summary>UI バージョンが違う場合（「新しいツール」6タイル画面）</summary>

お使いの UI のバージョンによっては、代わりに「**新しいツール**」画面（**プロンプト** / **エージェント フロー** / **コンピューターの使用** / **モデル コンテキスト プロトコル** / **カスタム コネクタ** / **REST API** の6タイル）が出ることがあります。その場合はタイル名が「**モデル コンテキスト プロトコル**」になっているので、それを選んでください。

![「新しいツール」6タイル画面の「モデル コンテキスト プロトコル」タイル](docs/images/cs_05_new_tool_tiles_variant.png)

</details>

**5. サーバー情報ダイアログに値を入力する**

![サーバー情報ダイアログに値を入力（URL はマスク済み）](docs/images/cs_06_mcp_dialog_filled.png)

> 💡 **この値は手で組み立てなくて OK。`copilot_studio_values.bat` をダブルクリック**すると、あなたの `.env` ＋ Dev Tunnel から実際の値をそのまま表示します（quickstart の STEP 5 でも自動表示）。コピペするだけです。

| 項目 | 入力値 |
|---|---|
| **サーバー名** | 任意（例: `companion`） |
| **サーバーの記述** | 任意（空でも可） |
| **サーバー URL** | `https://<your-tunnel>-8000.<region>.devtunnels.ms/mcp` （= `MCP_TUNNEL_URL` + `/mcp`） |
| **認証** | 「**API キー**」を選ぶ |
| **タイプ** | 「**ヘッダー**」を選ぶ（「クエリ」ではない） |
| **ヘッダー名** | `Authorization` |
| **API キーの値** | `Bearer <MCP_API_KEY の値>` （STEP 2 で表示された値） |

> ⚠️ **「API キーの値」欄には `Bearer ` という単語と半角スペースを含めて丸ごと貼ってください**（例: `Bearer 4baf1c2e...`）。ラベルが「API キー」なので生のキーだけを貼りがちですが、それだと 401 になります。`copilot_studio_values.bat` の出力はこの `Bearer ` 込みの行なので、その行をそのまま貼れば確実です。

**6. 「作成」をクリック** — 接続がテストされ、ツール一覧がロードされれば成功です。

**7. 「公開」→「利用者を自分だけ」に設定** — 必ず自分のみ。組織全体は絶対に選ばないでください。

**うまくいった目安:** ツール登録画面に `list_my_tools`, `read_file` などのツール名がずらっと表示されます。

登録が終わったらエージェントとチャットを開き、URL バーの URL を STEP 6 のダイアログに貼ります。

> ✅ **全部つながったか不安なら `doctor.bat` をダブルクリック。** サーバ→Dev Tunnel→専用 Edge→M365 サインイン→Bearer 認証まで全リンクを緑/赤でチェックし、赤にはその場で直し方を表示します。

---

## 毎日の使い方

- **起動**: デスクトップの「**M365 Companion**」アイコンをダブルクリックするだけ（quickstart が初回に作成）。サーバー・トンネル・Edge・UI を一括起動します。何度押しても安全です（冪等）。
- **2 つの窓**:
  - **CopilotChat** — 話しかける窓。手元のアプリのように Copilot と会話します。
  - **FleetCockpit** — 実行を見る窓。並列で走らせた作業をライブで監視・操作します。
- **書込・実行を初めて頼むと** `unlock(password=...)` を求められます。STEP 2 でメモした**アンロックパスワード**をエージェントに 1 度伝えれば、その端末は 30 日間解錠されます。
- **健康診断**: 調子が悪いと感じたら `doctor.bat` をダブルクリック。全リンクを緑/赤でチェックします。

---

## 困ったとき

まず **`doctor.bat` をダブルクリック**してください。**赤い行がそのまま直し方**です。それでも解決しない場合は下の FAQ と [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) を見てください。

**Q. Copilot が「申し訳ございません。それに応答できませんでした。」と返す**
A. エージェントの故障ではなく、MCP サーバーか Dev Tunnel が落ちている合図です。`doctor.bat` を実行すれば復旧手順が出ます。supervisor が動いていれば数十秒で自動復活します。

**Q. devtunnel のサインインが何度やっても通らない**
A. supervisor が動いていたら一度止め、新しいターミナルで `devtunnel login` を実行し、ブラウザの「続行」を確実にクリックしてください。

**Q. Copilot Studio でツール一覧が出ない**
A. Dev Tunnel の公開 URL が生成・host されているか確認してください（`doctor.bat` が判定します）。トンネルが落ちているとツールが読めません。

**Q. エージェントがツールを呼んでくれない**
A. Copilot Studio でエージェントに MCP コネクタの「接続を承認」が済んでいるか確認してください。認証（Bearer）が通っていないと呼び出せません。「API キーの値」に `Bearer ` を付け忘れていないかも確認を。

**Q. 途中で失敗した。やり直して大丈夫？**
A. `quickstart.bat` も `start_all.bat` も冪等です。**いつ何度実行しても安全**で、足りないものだけ補います。既に動いているトンネルは止めません。

---

## しくみ

```
[ M365 Copilot ] ──▶ [ Copilot Studio エージェント ] ──▶ [ devtunnel ]
                                                              ↓
                            [ MCP サーバー on あなたの PC ]
                                                              ↓
                          ファイル · Python · DB · Office生成 · OCR …
```

- あなたの PC で小さな Python サーバー（MCP サーバー）が動き、ファイルや Python 実行などの「手」を提供します。
- devtunnel が localhost のサーバーを安全な URL で公開し、Copilot Studio のエージェントがそこに繋ぎます。
- 別系統で、relay / fleet が Edge 上の Copilot Web UI を裏で駆動し、作業を無人で自走させます（あなたのマウス・キーボードは奪いません）。
- クラウド側（メール・予定表・SharePoint）は Copilot Studio の純正コネクタに任せ、ローカル PC 側をこの companion が担当する二枚重ねが本来の形です。

詳しい内部構造は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 性能

自律エージェントとして公式ベンチで実測した値です（詳細・再現手順は [docs/ADVANCED.md](docs/ADVANCED.md)）。

| ベンチ | 結果 | 備考 |
|---|---|---|
| **HumanEval（164 問）** | first-pass **98.2%**（161/164）/ 最終 **100%** | ground-truth 採点（隠しテスト再実行） |
| **SWE-bench Lite（300 件）** | **71.7%**（215/300） | 公式採点フル完走・grader 非リーク |
| **SWE-bench Verified（200 件）** | **76.5%**（153/200） | 別セットでの汎化確認（非 burned） |
| **GAIA（text-only 127 問）** | **70.1%**（89/127） | GAIA 公式スコアラ・既定 Copilot が回答 |

頭脳は M365 Copilot 内の Opus 4.8。数値は「頭脳が上」ではなく「スキャフォールドが効いている」ことを示します。スコアカードは `bench/` 配下（例: `bench/SCORECARD_swebench_lite300_strong.md`）。

---

## セキュリティ

- **Bearer 認証** — 固定 API キー（`MCP_API_KEY`）が無いと 401。当てずっぽうの bot は弾かれます。
- **unlock パスワード + IP 毎 TTL** — 書込・実行系ツールは IP 単位で解錠が必要（既定 30 日）。読取と変更で鍵が別なので、片方の漏洩だけでは変更系は通せません。
- **`MCP_ALLOWED_BASE` でファイル範囲制限** — エージェントが触れるフォルダの上限を設定でき、それ以外はブロックします。

詳細と注意点（トンネル匿名アクセス・データの流れ・退職時の掃除など）は [docs/SECURITY.md](docs/SECURITY.md)。

---

## もっと詳しく

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — コンポーネント解説・図・起動フロー・ポート一覧・リポジトリ構成
- [docs/CONFIG.md](docs/CONFIG.md) — `.env` 全キーのリファレンス
- [docs/SECURITY.md](docs/SECURITY.md) — 認証・認可モデルと運用上の注意
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — 症状別の直し方・doctor 各行の意味
- [docs/ADVANCED.md](docs/ADVANCED.md) — 全ツールカタログ・relay/fleet の詳細・ODBC・OpenAI 互換エンドポイント・ベンチ再現手順

---

## ライセンス

[MIT](./LICENSE)。無保証・無責任・自己責任でどうぞ。
