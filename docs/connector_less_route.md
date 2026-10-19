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
