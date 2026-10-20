日本語 | [English](#english)

# コネクタなし経路

## これは何か

通常、Copilot Studio のエージェントはカスタムコネクタ(MCP コネクタ)を持ち、Dev Tunnel を通してこの PC のツールに届きます。これが**コネクタ経路**です。

**コネクタなし経路**はコネクタもトンネルも使いません。エージェントはコネクタも自前のツールも持たない普通の Standard エージェントです。ツールを使いたいときはチャットの返信に次のようなコードブロックで呼び出しを書きます。

    ```call_tool
    {"name": "read_file", "arguments": {"path": "..."}}
    ```

この PC がそのブロックを読み取り、手元のサーバー(`http://127.0.0.1:8000`)に呼び出しを実行して、結果を次のチャットのメッセージで返します。外からこの PC に接続するものはないのでトンネルも公開 URL も登録するコネクタも要りません。

どちらの経路も使えます。既定はコネクタ経路のままなのでもう一方を選ばないマシンは何も変わりません。

## 必要なもの

- Copilot Studio を使える Microsoft 365 アカウント
- この製品のインストール(`quickstart.bat` を一度実行してあること)

Dev Tunnel CLI、トンネル、コネクタ、アクセス許可は要りません。

## 新しく使い始める人の手順

1. `quickstart.bat` を実行します。「How should Copilot Studio be allowed to reach this machine?」と聞かれたら **L**(connector-less)を押します。アクセスの質問と Dev Tunnel の手順は省かれます。
2. Copilot Studio で **Standard** エージェントを作ります。**Tools** の一覧は空のままにして、コネクタは追加しません。公開します。
3. 公開したエージェントを Microsoft 365 Copilot に追加します(Channels から Microsoft 365 Copilot)。そこでエージェントを開き、チャット画面のアドレスをアドレスバーからコピーします。
4. `quickstart.bat` が開くダイアログにそのアドレスを貼ります(あとから貼るなら `configure_env.bat`)。エージェントの URL として保存されます。
5. `docs/examples/connector_less_agent_instructions.txt` の文章を Copilot Studio のエージェントの **Instructions** に貼って、もう一度公開します。これがないと Microsoft のツールも付いているエージェントが、コードブロックによる呼び出しを断ることがあります(「そのツールは存在しません」と答える)。この文章を入れた状態では測定したすべての実行で呼び出しが守られました。
6. メールや予定表を使うなら同じエージェントに Copilot Studio で **Microsoft Work IQ** のツールを追加します。エージェントが初めてそのツールを使うときにチャットに同意カードが出ます。一度だけ承認してください。
7. `start_all.bat` を実行します(`quickstart.bat` を最後まで進めてもかまいません)。`doctor.bat` には Dev Tunnel の確認の代わりに「Tool route: connector-less」という情報の行が1つ出ます。

## すでにコネクタ経路で使っている人の移行手順

専用のインストーラーはありません。同じ製品で同じ `quickstart.bat` と `start_all.bat` を使います。スキル、フリート、記憶、設定はそのまま残ります。変わるのはエージェントと設定1つだけです。

1. **今のエージェントとコネクタはそのまま残します。** 戻るときに使います(「元に戻す」を見てください)。今使っているエージェントの URL(`MCP_IMPL_AGENT_URL`。`MCP_FLEET_AGENT_URL` を設定している場合はそれも)を控えておきます。
2. Copilot Studio で、**コネクタもツールも付けない**新しい **Standard** エージェントを作ります。Instructions に上の手順5の文章を貼り、メールや予定表が要るなら Work IQ のツールを追加して、公開します。そのうえで Microsoft 365 Copilot に追加し、チャット画面のアドレスをコピーします。今のエージェントからコネクタを外して使い回せるかは試していません。確認できているのは新しいエージェントを作る方法です。
3. `configure_env.bat` を実行して、エージェントの URL(フリート用を設定している場合はそれも)を新しいものに置き換えます。
4. コックピットの**設定**を開き、**ツール**の中の**ツールの接続方式**(英語表示では「Tool route」)を**コネクタなし**(英語表示では「Connector-less」)に切り替えます。フリートが次に作る会話から再起動なしで反映されます。すでに動いている会話は始まったときの経路のままです。
5. 動いている Dev Tunnel はそのままにします。supervisor はその管理をやめますが、動いているホストを止めることはありません。古いコネクタは削除するまでそのトンネルを指したままです。
6. 確認します。`doctor.bat` に「Tool route: connector-less」と出ること。そのうえで本番で頼る前にフリートに小さな作業を1つ(ファイルを読む、次にデータベースを読む)させてみてください。

この経路では Dev Tunnel もコネクタも要りません。元に戻すことはないと確信できてから Copilot Studio 側で削除してください。

## エージェントに渡す指示

コネクタ経路ではエージェントがサーバーに接続したときにサーバー自身の指示が渡ります。コネクタなしのエージェントには接続がないのでこの PC が動いているサーバーから同じ指示を読み取り、会話の最初に一度だけ(その会話ではそれ以降は送りません)、ツールの説明の前に付けて送ります。指示は定期的に読み直します。会話の開始時にサーバーに届かなかった場合は指示なしで会話を始め、ログに1行残します。

## メールと予定表

エージェントに追加した Work IQ のツールをそのまま使います。これらは `call_tool` の中にはありません。普通の Microsoft 365 のチャットと同じようにエージェントが自分で呼び出します。この PC が渡すツール一覧には名前が `outlook` で始まるツールは入れません。そのようなツールの呼び出しは断ります。この製品がデスクトップのメールソフトを起動することはありません。

## リスクと制限

- **呼び出しはモデルが書きます。** できることはコネクタ経路と同じですが、ブロックの書き間違い(項目の抜け、余計な文字)があるとそのターンは無駄になります。エラーはモデルに返され、たいていは自分で直します。
- **1ターンが遅くなります。** ツールを呼ぶたびにチャットが1往復します。このため、この経路ではフリートの無応答の上限とターンの上限を長くしています(既定は、無応答 600 秒、1ターン 1200 秒)。
- **チャットの長さの制限を受けます。** ツールの結果は切らずにそのままモデルへ返します。結果が非常に大きいと、モデル側の上限にあたってそのターンが失敗することがあります。
- **権限はコネクタ経路と同じです。** サーバー自身の確認(アンロック、ツールごとの権限、フォルダーの規則)が、すべての呼び出しにかかります。この経路で使える範囲が増えることはなく、確認が減ることもありません。
- **同意カードが出ます。** Work IQ のツールは初回にチャットで同意を求めます。承認するまでメールと予定表の依頼は失敗します。
- **すべての環境での動作は確認していません。** 試したのは短い作業と長い作業、データベースの読み取り、(Work IQ を付けた場合の)メールと予定表です。ほかのエージェント、テナント、ポリシーでは違う動きをするかもしれません。まず小さな作業で試してください。

## 元に戻す

**設定**の**ツール**を開き、**ツールの接続方式**(英語表示では「Tool route」)を**コネクタ(従来)**(英語表示では「Connector」)にします。そして `configure_env.bat` で控えておいた古いエージェントの URL を戻します。次の会話からまたコネクタ経路を使います。そのマシンでトンネルを一度も作ったことがなければもう一度 `quickstart.bat` を実行し、アクセスの質問に **A** か **T** と答えてください。省かれていた Dev Tunnel の手順が実行されます。

設定ファイルには、`tool_transport=connector` または `tool_transport=connector-less` として保存されます。以前の書き方 `tool_transport=bridge` もコネクタなしとして読み取ります。

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
