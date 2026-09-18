# The contract for an agent working through this server

This is the document an agent reads before deciding whether something it did worked. It exists
because the answer kept being decided by a database query instead of by the screen.

---

## 1. THE CONTRACT: if it cannot be confirmed in the GUI, it is a defect

**This applies to agent-facing paths exactly as it applies to a person's.** "The row is in
sqlite", "the endpoint returned `ok: true`", "the log says the turn completed" — none of these
are confirmation. The cockpit is what the owner looks at. If work reached the machine and the
cockpit cannot show it, the work is **not** done and the invisibility is **the defect**, not a
cosmetic gap to be noted and moved past.

Three consequences, all of which have been violated in this repository:

- **A submission you cannot see on screen is not a submission you can report.** Saying "queued
  successfully" on the strength of a JSON reply, while the panel shows "no fleet tasks", is a
  false report even when the JSON was accurate.
- **A verification run through the CLI is not a verification.** Reading `sessions.sqlite3` to
  prove a turn landed proves the row exists. It does not prove the path a person uses works.
  Measured 2026-09-18: a `/send` turn was confirmed by SQL and reported as end-to-end proof,
  and it was proof of a different path than the one asked about.
- **"It will appear shortly" is not an answer.** A gap between *accepted* and *visible* is a
  window in which nobody can tell a submission that landed from one that vanished. The fix is
  to show the accepted state, not to shorten the wait.

### Why this is stricter than it sounds

Fine defects are only found by going through the interface. This project's own history is the
argument: a disk floor was set by a constant and the screen said 2 while the effective value
was 0, for weeks, because the resolution order put a CLI flag above the setting and nobody had
gone through the GUI to notice. The same holds for agent paths — an agent that verifies its own
work by reading the store it just wrote to has proved that writing works, and nothing else.

---

## 2. Getting work onto the fleet, and the two routes that both exist

The server's instructions used to say that `fleet_submit` "needs no unlock and no gateway",
while RULE 4 of the same instructions says every tool lives behind `call_tool`. Both are true
of different clients and the pair reads as a contradiction, so an agent that could not see
`fleet_submit` in its own tool list concluded there was no route. **There are two, and which
one you have is decidable:**

**Route A — direct, when your tool list shows it.**

```
fleet_submit(goal="<the whole instruction, standalone>", source="<who asked>")
```

**Route B — through the gateway, when it does not.** Most clients cap the tool list, and this
server has ~170 tools, so `fleet_submit` being absent from your list is **expected** and proves
nothing:

```
call_tool(name="fleet_submit", arguments={"goal": "<...>", "source": "<who asked>"})
```

**How to tell which you have, without guessing:** call `call_tool(name="fleet_submit")` with no
arguments. It returns that tool's signature if it exists, and the closest matching names if you
have the name wrong. Either answer is progress; neither costs a run.

Neither route needs `unlock`. `fleet_submit` queues; it does not execute.

### What to say after submitting, and what not to say

`fleet_submit` returns a job id. That means **queued**, not **started** — the reply says so in
those words. Per §1, the submission is confirmed when it is visible on the cockpit: a queued
job now appears there immediately, as *投入済み / unclaimed* with its id and age, and moves to a
worker row once a coordinator picks it up. Before 2026-09-18 it appeared only once a worker
existed, and the panel said "タスクはまだありません" in the meantime — which was not slow, it
was false.

### `source` is not decoration

`source=<who asked>` travels with the job into `.fleet/tasks/done/<id>.json` as
`origin.source`, and it is the only record of where an instruction came from. A job submitted
over the tunnel by an agent is not the same authority as one a person typed into the cockpit,
and the consumer is entitled to treat them differently. Measured 2026-09-18: a job that ran at
03:33 was traced to its requester **only** because this field had been filled in.

---

## 3. Reading a picture, and which tool sees what

Covered in full in [`architecture/showing_a_picture_to_a_model.md`](architecture/showing_a_picture_to_a_model.md).
The short version, because the wrong choice here produced fabricated answers twice in one run:

| want | use |
|---|---|
| text on a plain background | `ocr_image(path)` — exact and cheap |
| size, a colour, whether a region is blank | `run_python` with PIL/numpy — deterministic |
| what the picture shows | `read_image(path)` returns an image block a vision-capable client renders |
| the same, through Copilot itself | `ANALYZE: <abs path> \| <instruction>` as the last line of a turn — **runs on a model nobody can pick**, so ground-verify any number it gives you |

---

## 4. Refusals, and the one that means what it says

A refusal that names a missing `unlock_token` means exactly that: include the token, do not
retry the same call unchanged and do not fall back to read-only tools and present the result as
though the task were done.

A refusal that names a **locked** state is recorded as a failure, not a success, whatever the
transport reported — the ledger corrects `ok=True` to `ok=False` when the result text is a lock
refusal, because 320 rows of 11,686 had been filed as successes and every recovery rate
computed over them was wrong.
