日本語 | [English](#english)

# コネクタなし経路

## 概要

既定では、Copilot Studio のエージェントはカスタムコネクタ(MCP コネクタ)を持ち、Dev Tunnel を通じてこの PC のツールに届きます。これが**コネクタ**経路です。

**コネクタなし**(connector-less)経路は、そのどちらも必要としません。エージェントは、コネクタもエージェント自身のツールも持たない通常の Standard エージェントです。ツールを使いたいとき、エージェントはチャットの返信にフェンス付きブロックとして呼び出しを書きます。

    ```call_tool
    {"name": "read_file", "arguments": {"path": "..."}}
    ```

この PC はそのブロックを読み取り、自分のローカルサーバー(`http://127.0.0.1:8000`)に対して呼び出しを実行し、結果を次のチャットメッセージで返します。外部からこの PC に接続するものは何もないので、トンネルも、公開 URL も、登録するコネクタもありません。

どちらの経路もサポートされています。既定はコネクタ経路のままで、もう一方を選ばないマシンには何も変わりません。

## 必要なもの

- Copilot Studio を使える Microsoft 365 アカウント。
- この製品がインストール済みであること(`quickstart.bat` を一度実行済み)。

Dev Tunnel CLI、トンネル、コネクタ、アクセス許可は不要です。

## 新規ユーザーのセットアップ

1. `quickstart.bat` を実行します。「How should Copilot Studio be allowed to reach this machine?」という質問が出たら、**L**(connector-less)を押します。アクセスに関する質問と Dev Tunnel の手順はスキップされます。
2. Copilot Studio で **Standard** エージェントを作成します。**Tools** の一覧は空のままにします。コネクタは追加しません。公開します。
3. 公開したエージェントを Microsoft 365 Copilot に追加し(Channels、続いて Microsoft 365 Copilot)、そこで開きます。チャットページのアドレスをアドレスバーからコピーします。
4. `quickstart.bat` が開くダイアログにそのアドレスを貼り付けます(または後で `configure_env.bat` を実行します)。エージェント URL として保存されます。
5. `docs/examples/connector_less_agent_instructions.txt` のテキストを、Copilot Studio のエージェントの **Instructions** に貼り付け、もう一度公開します。これがないと、Microsoft のツールも追加されているエージェントは、フェンス付きブロックの約束事を拒否することがあります(「that tool does not exist」)。これがあると、測定したすべての実行で約束事が守られました。
6. メールと予定表を使うには、同じエージェントに Copilot Studio で **Microsoft Work IQ** ツールを追加します。エージェントがそれを初めて使うとき、チャットに同意カードが表示されます。一度だけ承認します。
7. `start_all.bat` を実行します(または `quickstart.bat` を最後まで進めます)。`doctor.bat` は、Dev Tunnel の確認の代わりに、情報としての1行「Tool route: connector-less」を表示します。

## すでにコネクタ経路を使っているインストールの移行

別のインストーラーはありません。同じ製品で、同じ `quickstart.bat` / `start_all.bat` です。スキル、フリート、メモリ、設定はそのまま残ります。変わるのはエージェントと設定1つだけです。

1. **古いエージェントとそのコネクタはそのままにします。** 元に戻す手段になります(「元に戻す」を参照)。現在使っているエージェント URL(`MCP_IMPL_AGENT_URL`、設定している場合は `MCP_FLEET_AGENT_URL` も)を控えておきます。
2. Copilot Studio で、**コネクタもツールもない**新しい **Standard** エージェントを作成し、Instructions のテキスト(上の手順5)を貼り付け、メールと予定表が必要なら Work IQ ツールを追加し、公開し、Microsoft 365 Copilot に追加して、そのチャットのアドレスをコピーします。古いエージェントからコネクタを外す方法は試していません。確認済みの方法は新しいエージェントです。
3. `configure_env.bat` を実行し、エージェント URL(設定している場合はフリートのエージェント URL も)を新しいものに置き換えます。
4. コックピットの**設定**を開き、**ツール**を見つけて、**ツールの接続方式**(英語表示では "Tool route")を**コネクタなし**(英語表示では "Connector-less")にします。フリートが次に作る会話から、再起動なしで反映されます。すでに実行中の会話は、始まったときの経路のままです。
5. すでに動いている Dev Tunnel はそのままにします。スーパーバイザーはそれの管理をやめますが、稼働中のホストを停止することはありません。古いコネクタは、削除するまでそれを指し続けます。
6. 確認: `doctor.bat` が「Tool route: connector-less」を表示します。そのうえで、頼りにする前に、フリートに小さな作業を1つ与えます(ファイルを読む、続いてデータベースを読む)。

この経路では Dev Tunnel とコネクタはもう不要です。元に戻すことはないと確信できてから、Copilot Studio で削除してください。

## エージェントに伝えること

コネクタ経路のエージェントは、接続したときにローカルサーバー自身の指示を受け取ります。コネクタなしのエージェントには接続がないので、この PC が稼働中のサーバーから同じ指示を読み取り、会話の開始時に一度だけ(その会話では二度と送らず)、ツールの説明の前に送ります。指示は定期的に更新されます。会話の開始時にサーバーに届かなかった場合は、指示なしで会話を始め、ログに1行が記録されます。

## メールと予定表

エージェントに追加したネイティブの Work IQ ツールを使います。これらは `call_tool` の背後にはありません。エージェントが、通常の Microsoft 365 チャットと同じように自分で呼び出します。この PC が提示するツール一覧には、名前が `outlook` で始まるツールは含まれず、そのようなツールへの呼び出しは拒否されます。デスクトップのメールクライアントがこの製品によって起動されることはありません。

## リスクと制限

- **呼び出しはモデルが書きます。** ここでできることはコネクタ経路でもすべてできますが、ブロック内の誤り(項目の欠落、余分な文字)があると1ターンを失います。ループはエラーをモデルに返し、モデルはたいてい訂正します。
- **ターンが遅くなります。** ツール呼び出しのたびにチャットを1往復します。そのため、この経路ではフリートの無応答の上限とターンの上限が長くなっています(既定でアイドル 600 秒、1ターンあたり 1200 秒)。
- **チャットの長さの制限が適用されます。** 非常に大きな結果は、モデルに返す前に切り詰められ、そのことがモデルに伝えられます。
- **コネクタと同じ権限です。** ローカルサーバー自身のチェック(アンロック、ツールごとの権限、フォルダーポリシー)がすべての呼び出しに適用されます。この経路が新しいアクセスを加えることはありませんが、チェックを外すこともありません。
- **同意カード。** Work IQ ツールは、初回にチャットで同意を求めます。承認するまで、メールと予定表のリクエストは失敗します。
- **まだすべての環境で実証されてはいません。** 短い作業と長い作業、データベースの読み取り、(Work IQ を追加した場合の)メールと予定表を試しました。ほかのエージェント、テナント、ポリシーでは挙動が異なる可能性があります。まず小さな作業を実行してください。

## 元に戻す

**設定**、**ツール**を開き、**ツールの接続方式**(英語表示では "Tool route")を**コネクタ(従来)**(英語表示では "Connector")にして、`configure_env.bat` で古いエージェント URL を元に戻します。次の会話から、再びコネクタ経路を使います。そのマシンにトンネルがあったことがない場合は、もう一度 `quickstart.bat` を実行し、アクセスの質問に **A** または **T** で答えてください。スキップされていた Dev Tunnel の手順が設定されます。

この設定は、設定ファイルに `tool_transport=connector` または `tool_transport=connector-less` として保存されます。以前の表記 `tool_transport=bridge` は、引き続き connector-less として読み取られます。

---

<a id="english"></a>

# The connector-less route

## What it is

By default the agent in Copilot Studio holds a custom connector (an MCP connector) and reaches this
PC's tools through a Dev Tunnel. That is the **connector** route.

The **connector-less** route needs neither. The agent is a plain Standard agent with no connector and
no tools of its own. When it wants a tool it writes the call in its chat reply, as a fenced block:

    ```call_tool
    {"name": "read_file", "arguments": {"path": "..."}}
    ```

This PC reads the block, runs the call against its own local server (`http://127.0.0.1:8000`),
and sends the result back in the next chat message. Nothing connects to this PC from outside, so there
is no tunnel, no public URL and no connector to register.

Both routes are supported. The connector route stays the default; nothing changes for a machine that
does not choose the other one.

## What you need

- A Microsoft 365 account that can use Copilot Studio.
- This product installed (`quickstart.bat` has run once).

You do not need the Dev Tunnel CLI, a tunnel, a connector, or an access grant.

## Setup for a new user

1. Run `quickstart.bat`. At the question "How should Copilot Studio be allowed to reach this
   machine?" press **L** (connector-less). The access question and the Dev Tunnel step are skipped.
2. In Copilot Studio create a **Standard** agent. Leave its **Tools** list empty: do not add a
   connector. Publish it.
3. Add the published agent to Microsoft 365 Copilot (Channels, then Microsoft 365 Copilot), and open it
   there. Copy the chat page's address from the address bar.
4. Paste that address into the dialog `quickstart.bat` opens (or run `configure_env.bat` later). It is
   saved as the agent URL.
5. Paste the text of `docs/examples/connector_less_agent_instructions.txt` into the agent's
   **Instructions** in Copilot Studio and publish again. Without it, an agent that also has Microsoft
   tools attached can refuse the fenced-block protocol ("that tool does not exist"); with it, the
   protocol was followed in every measured run.
6. For mail and calendar, attach the **Microsoft Work IQ** tools to the same agent in Copilot Studio.
   The first time the agent uses one, a consent card appears in the chat: approve it once.
7. Run `start_all.bat` (or let `quickstart.bat` finish). `doctor.bat` shows one informational line,
   "Tool route: connector-less", in place of the Dev Tunnel checks.

## Moving an install that already uses the connector route

There is no separate installer: it is the same product and the same `quickstart.bat` / `start_all.bat`.
Your skills, fleet, memory and settings stay as they are. Only the agent and one setting change.

1. **Keep the old agent and its connector as they are.** They are your way back (see "Switching back").
   Write down the agent URL you use today (`MCP_IMPL_AGENT_URL`, and `MCP_FLEET_AGENT_URL` if you set one).
2. In Copilot Studio create a new **Standard** agent with **no connector and no tool**, paste the
   Instructions text (step 5 above), attach the Work IQ tools if you want mail and calendar, publish it,
   add it to Microsoft 365 Copilot and copy its chat address. Taking the connector off the old agent
   has not been tested; a new agent is the verified path.
3. Run `configure_env.bat` and replace the agent URL (and the fleet agent URL, if set) with the new one.
4. Open the cockpit's **Settings**, find **Tools**, set **Tool route** to **Connector-less**. It applies
   to the next conversation the fleet creates, with no restart; conversations already running keep
   the route they started with.
5. The Dev Tunnel that is already running is left alone: the supervisor stops managing it but never
   stops a live host. The old connector keeps pointing at it until you remove it.
6. Check: `doctor.bat` shows "Tool route: connector-less"; then give the fleet one small task (read a
   file, then a database read) before you rely on it.

The Dev Tunnel and the connector are not needed any more on this route; remove them in Copilot Studio
only when you are sure you will not switch back.

## What the agent is told

A connector agent receives the local server's own instructions when it connects. The connector-less
agent has no connection, so this PC reads the same instructions from the running server and sends
them once, at the start of each conversation (never again in that conversation), ahead of the tool
description. They are refreshed periodically; if the server cannot be reached when a conversation
starts, it starts without them and a line is logged.

## Mail and calendar

Use the native Work IQ tools attached to the agent. They are not behind `call_tool`: the agent calls
them itself, the way it would in any Microsoft 365 chat. The tool list this PC offers never contains
a tool whose name starts with `outlook`, and a call to one is refused; the desktop mail client is
never started by this product.

## Risks and limits

- **The model writes the call.** Everything it can do here it can also do on the connector route, but
  a mistake in the block (a missing field, a stray character) costs a turn. The loop reports the error
  back to the model, which usually corrects it.
- **Slower turns.** Each tool call is a chat round trip. The fleet's silence and turn limits are longer
  on this route (600 s idle, 1200 s per turn by default) for that reason.
- **Chat-length limits apply.** Very large results are truncated before they go back to the model, and
  the model is told so.
- **Same permissions as the connector.** The local server's own checks (unlock, per-tool permission,
  folder policy) apply to every call. The route adds no new access, but it also does not remove any
  check.
- **Consent cards.** Work IQ tools ask for consent in the chat the first time. Until approved, mail and
  calendar requests fail.
- **Not yet proven everywhere.** Short and long tasks, a database read and (with Work IQ attached) mail
  and calendar were exercised. Other agents, tenants and policies may behave differently; run a small
  task first.

## Switching back

Open **Settings**, **Tools**, set **Tool route** to **Connector**, and put the old agent URL back with `configure_env.bat`. The next conversation uses the
connector route again. If the machine has never had a tunnel, run `quickstart.bat` again and answer
the access question with **A** or **T**; it sets up the Dev Tunnel step that was skipped.

The setting is stored as `tool_transport=connector` or `tool_transport=connector-less` in the settings
file. The earlier spelling `tool_transport=bridge` is still read as connector-less.
