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
5. For mail and calendar, attach the **Microsoft Work IQ** tools to the same agent in Copilot Studio.
   The first time the agent uses one, a consent card appears in the chat: approve it once.
6. Run `start_all.bat` (or let `quickstart.bat` finish). `doctor.bat` shows one informational line,
   "Tool route: connector-less", in place of the Dev Tunnel checks.

If you already installed with the connector route, open the cockpit's **Settings**, find **Tools**, and set
**Tool route** to **Connector-less**. The change applies to the next conversation the fleet creates, with
no restart; conversations already running keep the route they started with. Then follow steps 2 to 5.

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

Open **Settings**, **Tools**, set **Tool route** to **Connector**. The next conversation uses the
connector route again. If the machine has never had a tunnel, run `quickstart.bat` again and answer
the access question with **A** or **T**; it sets up the Dev Tunnel step that was skipped.

The setting is stored as `tool_transport=connector` or `tool_transport=connector-less` in the settings
file. The earlier spelling `tool_transport=bridge` is still read as connector-less.
