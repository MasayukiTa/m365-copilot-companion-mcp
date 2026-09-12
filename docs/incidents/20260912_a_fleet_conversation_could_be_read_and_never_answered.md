# A fleet conversation could be read and never answered

**2026-09-12.** Reported by the operator, with a screenshot: a follow-up typed into an open
fleet conversation in the chat window is refused with

> この会話の送信先を特定できません。会話を開き直してください。

and reopening changes nothing — because reopening supplies nothing that was missing. The
refusal is 100% reproducible for every fleet conversation, not a race; the operator noted it
happens "完了後即投下でも".

## What was actually missing

`SendText` picks a send target through three doors, and a fleet row holds no key to any:

| door | needs | a fleet row has |
|---|---|---|
| `/switch` | `ConvUrl` | `""` |
| `/resume` | `Source=="chat"` and a sid in `Name` | `Source="fleet"`, `Name="w0"` (a worker name) |
| `/new` | `Messages.Count == 0` | the transcript is loaded |

so it fell through all three to `send_unknown_conv`. The same empty `ConvUrl` also left
`_activeFleetUrl` empty, so steer mode never armed and `RefreshFleetSnapshot` returned at its
first line. **One missing field, three symptoms.**

### Why `ConvUrl` was empty

`relay_fleet._capture_url`'s entire body sat under `if self.page is not None`. A socket worker
has no page — that is what a socket is — so it never recorded the conversation it was in, and
`_tx.note_guid` (which writes the guid into the transcript) was only ever reached from that
same page branch. The socket path is the ordinary path now, so this was nearly all of them.

Measured on the live machine:

```
.fleet/status.json         w0            conv_url ""
.fleet/conversations.json  267 fleet rows, url "" on all but one
.fleet/transcripts/*.jsonl keys: goal key meta name role text ts turn   -- no `guid`
.fleet/socket_route.jsonl  worker_done   conv_client d3710cc2-a5ff-4d74-9c0e-027e96773cb9
```

The identity existed and was durable the whole time. Nothing copied it into the field every
consumer reads. `socket_driver.conversation_ids`' own docstring names the gap exactly — *"NOT
PERSISTENCE — just the ability to be asked … 531 of the 542 sessions on this machine carry no
way back to the conversation they were about"* — the ability was built and nothing ever asked.

### The workaround was not doing what it looked like

The operator's read was that continuing through the fleet works ("フリート経由は再度立ち上げ
なので当然続き行ける"). It was a new conversation with the context re-pasted by hand:

```
worker_done  original goal   -> conv_client f3e832b7-ab2e-4373-8b80-040198db7811
worker_done  follow-up goal  -> conv_client d3710cc2-a5ff-4d74-9c0e-027e96773cb9
```

Two different conversations. `follow_up_to` — the field that would have made it one — had a
single producer (`fleet_runner._follow_up`) and a single consumer
(`RelayWorker.__init__`), and the only path between them ran **inside one live run**:
`goals_from_command` dropped the key, so nothing arriving through the command channel could
ask for a continuation. Accepted, silently downgraded to a fresh chat, plausible either way.
This is the writer/reader mismatch that function's own docstring warns about, in that
function.

## The three doors were the wrong doors anyway

A fleet conversation does not live on the bridge page; it lives in a worker on a socket.
Pulling `PAGE` onto it would be the wrong answer even if a URL existed. The fleet already
accepts a message for one of its conversations, in both states it can be in:

| worker state | channel | effect |
|---|---|---|
| live | `commands.json` `{"steer": {...}}` | `deliver_steers` hands it over; takes effect on the next turn |
| finished / older run / no fleet up | `commands.json` `add_goal` with `follow_up_to` | `RelayWorker.__init__` resolves it through `socket_route.conversation_for_goal` and continues the same conversation |

Both mechanisms existed and had no caller from the chat window. The fix adds the caller.

**Addressing is the part that can go quietly wrong.** A live worker is addressed by *name*,
and `w0` exists in every run there has ever been — steering an old conversation by name would
interrupt a stranger and look delivered. So the live branch is taken only when the *current*
`status.json` lists a worker whose **transcript path** is this conversation's: an identity
join, not a name match. Everything else goes to the follow-up, addressed by goal text, which
is safe to write at any time.

## Changes

- `relay/relay_fleet.py` — a socket worker records `conv_url = "sess:<guid>"` and notes the
  guid on its transcript. Client id preferred (it is the one that appears in the page URL, and
  the one `conversation_for_goal` matches). Idempotent, never overwrites, never raises.
- `relay/fleet_runner.py` — `goals_from_command` carries `follow_up_to` through, so the command
  channel can name the conversation to continue.
- `ui/CopilotChat.cs` — a fleet conversation is routed to the fleet instead of the page doors;
  `DiscoverTranscripts` keeps the full goal text and reads the transcript's guid line;
  `commands.json` is merged rather than overwritten.

## Regression detection

- `relay/test_socket_conversation_identity.py` — seven new cases on the recording, plus the
  command-channel passthrough. **Negative control run:** four of them fail against the
  pre-fix source and pass after.
- `ui/test_a_fleet_conversation_can_be_answered.py` — routing order, transcript-path
  addressing, goal-text follow-up, command-file merge, and a check that the deployed
  `CopilotChat.exe` is not behind its source (a source assertion cannot see a stale build).

## Still open

Transcripts written before this change carry no guid line, so conversations from earlier runs
remain unresumable — the identity was never written and cannot be recovered from the
transcript. `.fleet/socket_route.jsonl` still holds `conv_client` keyed by goal text for many
of them, so a back-fill is possible; it has not been done.
