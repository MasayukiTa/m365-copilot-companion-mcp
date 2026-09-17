# Showing a picture to a model, and attaching a file over the socket

Two questions that keep being re-derived from scratch. Both have measured answers and both
were, until 2026-09-18, recorded only in temp-directory scratch files and in one code comment.
This is the durable copy.

---

## 1. Which tool to use on an image

| want | use | measured |
|---|---|---|
| text on a plain background | `ocr_image(path)` | returned the six characters exactly, on the file `read_image` was fabricating about. Cheap. |
| pixel facts — size, a colour at a point, whether a region is blank | `run_python` with PIL/numpy | deterministic |
| what the picture SHOWS — layout, coordinates, which application is on screen | `ANALYZE: <abs path> \| <instruction>` as the last line of a turn | the only path in this repository that puts a file in front of a model. **See the caveat below — it is not a strong one.** |
| nothing | `read_image(path)` | returns a base64 data URI as TEXT. **Nothing in this stack renders it.** |

### `read_image` returns something no one sees

`read_image` is typed `-> str`; FastMCP serialises a `str` as a text content block, and no
client here turns that into a picture. The return value *looks* successful, which is the
problem: on 2026-09-17 a worker asked to read six characters off an image called it and then
answered a string that was not on the image — **twice**, describing both times how it had
looked. The refuter caught both. `ocr_image` on the same file returned the six characters.

Cost, from `.fleet/tool_events.jsonl` over 424 calls: **median 142,642 characters returned**,
p90 1,166,294, max 1,762,326. Seventeen calls in one 34-minute run is the observed volume. Ten
of eighteen worker transcripts in one run died of conversation token limits. Still in live use:
445 calls in the last 7 days, 12 in the last 24 hours (measured 2026-09-18).

`tools/test_a_cap_in_bytes_never_bound_the_thing_that_broke.py` pins the `data:image/` prefix
in three places. It was written for a different defect — `MAX_BYTES` caps the FILE at 8 MB,
about 10.7 million characters of base64, so it had never once been the binding constraint —
so changing the return value means deciding what those three assertions should say instead.

### THE ANALYST CAVEAT — read this before recommending `ANALYZE` for a judgement

`relay/agent_profiles.py` has said this since it was written, and it is easy to quote the
"only path that shows a picture to a model" half without it:

> Analyst ("アナリスト") analyses an UPLOADED data file. Unlike the Researcher it has **NO model
> picker (cannot be switched to Claude) and runs on its default model.** … Per spec §5 its
> numeric claims **must be ground-verified with the local tools**.

So `ANALYZE` is the only vision path AND it lands on an unselectable default model. It has been
measured reproducing text off an image faithfully (4434 and 6568 characters, including the 備考
column), which is a transcription result and says nothing about reasoning quality. For
"where exactly is this control" or "which application is this", the answer comes back from a
model nobody chose and cannot be switched.

**This is the open gap, not a solved problem.** Deciding what to do about `read_image` is
therefore *not* just "make it refuse and point at ANALYZE": the uses that genuinely need a
vision model — coordinates for computer-use, identifying an application — have no strong path
today. `ocr_image` and `run_python` do not cover them.

`RESEARCHER` does have a model picker
(`button[data-testid="researcher-model-picker-button"]`); `ANALYST` and `PLAIN` have
`model_picker=None`.

### Also on the Analyst

`min_report_chars=4` (not the module default of 1000). An analyst answer can legitimately BE
six characters, and the 1000-character floor made every short `ANALYZE` time out at 600 s with
the answer already on screen (measured 2026-09-17). What separates a status line from an answer
is `_is_processing` — a known progress marker AND under 40 characters — which runs one branch
earlier.

---

## 2. A socket DOES have somewhere to put a file

`relay/transport_policy.py` routed file work away from the socket on the reasoning that "a
socket has nowhere to put a local file". That reasoning is wrong. The routing is still in
force deliberately, for a different reason given below.

### What was observed, and how

**Recorded 2026-09-17 13:37 by watching the page's own CDP websocket frames while an `ANALYZE`
attachment ran.** That is worth stating plainly because it is the part that was missing: the
upload was produced *by running an ANALYZE*, not by a hand-upload and not by any of the
forbidden bridge page endpoints. Anyone repeating this starts a recorder and then runs an
ANALYZE on any image.

```
POST https://substrate.office.com/m365Copilot/UploadFile     (multipart, scenario=UploadImage)
```

and the outgoing ChatHub frame then carried:

```json
"messageAnnotations": [{
  "id": "0-wjp-d3-...",
  "messageAnnotationMetadata": {"@type": "File", "fileType": "png", "fileName": "monthly.png"},
  "messageAnnotationType": "ImageFile"
}]
```

So the bytes go over HTTP and an **id rides the socket**. The `<input type=file>` is the UI's
door, not the protocol's requirement.

### What is still unanswered

Does the token the relay already captures get past that door? It has audience
`substrate.office.com/sydney` — the same host as `UploadFile`, a different path. Until that is
answered the routing stays as it is.

Readings, decided in advance so the result cannot be talked into being a pass:

- **200/201** — the relay can upload. The id in the response is what a socket frame carries in
  `messageAnnotations`, and the Analyst can leave the tab behind.
- **401/403** — the audience does not cover this path. Not a dead end: the next question is
  which audience does, which is a different experiment.
- **400** — authorised, and the body is wrong. That is a **pass** on the question being asked;
  the multipart field list is then the remaining work.

### The two probes

Both live in `scripts/probes/`.

- `record_upload_call.py` — **passive.** Attaches to CDP (`http://127.0.0.1:9222`), watches
  every page target's frames, and records the multipart field list and what `UploadFile`
  returns. Records nothing about the token: an earlier draft decoded the `Authorization`
  header to judge its audience, and discarding the bytes afterwards does not change what that
  is. Runs for 1200 s by default (`record_upload_call.py <seconds>`). **It captures nothing
  unless an upload happens inside the window — run an `ANALYZE` to produce one.** Two runs
  recorded `start`/`watching`/`stop` and nothing else, for exactly this reason.
- `can_we_upload.py` — **active, and the one that answers the open question.** Captures a token
  through the repository's own `relay/profile_token.token_via_light_page` (open a light page,
  watch our own outgoing requests, close it), POSTs one generated 240×90 PNG to `UploadFile`
  with `scenario=UploadImage` and an invented `conversationId`, and prints the status and the
  response shape with token-shaped fields removed. The token lives in a local variable for the
  length of one request and is never written down. Nothing is sent to a model, no conversation
  is created, no page is left open.

### On the permission classifier

`record_upload_call.py` is **not** blocked — measured 2026-09-18: it started and attached to a
CDP target under auto mode. An earlier report that "the UploadFile verification is blocked by
the classifier" conflated the two scripts. `can_we_upload.py` is the one that captures a
credential, and it is the one that drew the "Credential Exploration" verdict.
