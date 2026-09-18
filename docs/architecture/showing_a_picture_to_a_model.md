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
| the picture itself | `read_image(path)` | **fixed 2026-09-18**: returns an IMAGE content block, which a vision-capable MCP client renders. Until then it returned a base64 data URI as TEXT and nothing rendered it. Not measured through the fleet's browser UI. |

### `read_image` returned something no one saw, until the return TYPE was fixed

`read_image` WAS typed `-> str`; FastMCP serialises a `str` as a text content block, and no
client turned that into a picture. The return value *looked* successful, which is the
problem: on 2026-09-17 a worker asked to read six characters off an image called it and then
answered a string that was not on the image — **twice**, describing both times how it had
looked. The refuter caught both. `ocr_image` on the same file returned the six characters.

Cost, from `.fleet/tool_events.jsonl` over 424 calls: **median 142,642 characters returned**,
p90 1,166,294, max 1,762,326. Seventeen calls in one 34-minute run is the observed volume. Ten
of eighteen worker transcripts in one run died of conversation token limits. Still in live use:
445 calls in the last 7 days, 12 in the last 24 hours (measured 2026-09-18).

**The fix, 2026-09-18.** It returns a `fastmcp.utilities.types.Image`, which FastMCP
serialises as an IMAGE content block. The first conclusion drawn — make it refuse and point at
the alternatives — was wrong: that removes a capability because its plumbing is broken, which
makes the broken plumbing permanent. A visual path is not optional, because computer-use
decides where to click and a one-pixel error is a miss, so nothing built on OCR or on pixel
arithmetic replaces seeing the screen.

`tools/test_a_cap_in_bytes_never_bound_the_thing_that_broke.py` used to pin the `data:image/`
prefix in three places. It was written for a different defect — `MAX_BYTES` caps the FILE at
8 MB, about 10.7 million characters of base64, so it had never once been the binding
constraint — and it now reads the same quantities off the image block, because an ImageContent
carries the bytes base64-encoded and the ceiling bounds the same currency either way.

**Not measured through the fleet's own workers**, who reach this through a browser UI this
repository does not control. For them `ocr_image` and `ANALYZE` remain the measured answers.

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

### Answered, 2026-09-18: **200**, and the token was never the problem

The page's own `UploadFile` request, re-issued with **only** the `Authorization` header swapped
for the token `relay/profile_token` captures:

```
HTTP 200
{"docId":"0-ejp-d1-...","fileSanitizer":"ImageSanitizerBingAI",
 "result":{"value":"Success","message":"Success"}}
```

The credential is not what blocks the socket route. The `docId` is what rides the socket as
`messageAnnotations[0].id` — and that response was captured for the first time the same day,
which is also when "the page uploaded successfully" stopped being an inference from a POST plus
a later annotation.

**Three attempts, and the first two were reported as answers.** Both returned 403 and neither
was about the credential:

| # | what differed from the page's request | result |
|---|---|---|
| 1 | binary part named `file`, invented `conversationId`, our token | 403 |
| 2 | the right three fields, a real `conversationId`, our token | 403 |
| 3 | **nothing but the token** — the page's own bytes and headers | **200** |

What #1 and #2 were missing: `optionsSets` **three times**, and seventeen of the eighteen
headers the page sends — including `x-anchormailbox`, which routes the request, and
`origin`/`referer`, which many Microsoft endpoints check.

The three-field list came from a **400-character truncated** `body_head`, and a test had been
written to pin agreement with it — a guard holding a probe to an incomplete record. Auth shape,
measured rather than assumed: **Bearer, no cookie.**

**Reconstructing the request is what failed, every time.** The lesson is in the probe's shape,
not in a comment: `replay_upload_with_our_token.py` captures the page's request and re-issues
it. The page's own credential is replaced before the request is built, never read and never
written; the forwarded headers stay in memory because several identify the account.

**Not asked, and 200 does not answer:** how long a `docId` lives, and whether an annotation sent
over the socket is accepted. Until those, an attachment still goes to a tab — now because two
specific questions are open, not because the door was thought to be shut.

### Answered end to end, 2026-09-18: the socket carries an attachment

An image carrying a randomly generated phrase was uploaded through the page's own request with
**only** the `Authorization` header swapped, its `docId` was sent as `messageAnnotations` on a
socket turn, and the reply read the phrase back. It appears in no filename, no path and no
prompt, so it can only have come from the pixels.

So all three questions that kept attachments on a tab are measured:

| question | answer |
|---|---|
| does the protocol have a place for it | yes — 2026-09-17, and the server echoed it back as `UserAnnotated` |
| does our credential open `UploadFile` | yes — HTTP 200, `result.value: "Success"` |
| does a model on the socket **see** it | yes — it read the phrase back |

`relay/transport_policy`'s `ATTACHMENT` rule is retired. `needs_tab()` returns False for
everything, and the call stays ahead of every version because **the ordering is the asset**: a
structural veto found later must bind versions that already exist, including evolved ones.

**The annotation was added to the CAPTURED template, not composed into a frame.** That
distinction is the whole reason it worked — this protocol rejects a composed frame and accepts
the client's own, which `relay/chathub.py` had already measured on 2026-08-20.

**The first attempt was scored wrong, and the way it was wrong is worth keeping.** The probe drew
six random characters inside a box — a CAPTCHA — and the model refused on exactly those grounds.
That refusal was proof the picture had arrived, and the probe called it a failure because its
only test was "is the token in the reply". A model cannot refuse an image it never received.

### The probes

All live in `scripts/probes/`, and `test_the_probe_matches_the_recording.py` holds them to the
observation.

- **`observe_real_upload.py`** — makes the page do it, with a listener attached. Opens the
  Analyst surface, puts a file into its own `<input type=file>` exactly as
  `relay/agent_profiles.upload_file` does, and stops: no turn is sent, no question asked,
  nothing reaches a model. Records header *names*, whether an `Authorization` header is present
  and its **scheme word only**, whether a `Cookie` header rides along, the multipart field
  names, and the response status and body. No header value, no token decoded, and the raw CDP
  event — which carries every header and every cookie — is never stored. Prints tab counts
  before and after and closes its page in a `finally`, because one endpoint touched carelessly
  here once lit a self-feeding loop that reached 40 Edge processes and 4.3 GB.

- **`replay_upload_with_our_token.py`** — the one-variable experiment, and the only one whose
  result means anything. Captures the page's own request — bytes and headers, as sent — and
  re-issues exactly that with the `Authorization` header replaced and nothing else. The page's
  own credential is replaced *before* the request is built, so it is never read, logged or
  re-sent; the other headers pass through in memory and are not written to disk, because
  several identify the account. What lands in the output file is the status, the body with
  token-shaped keys scrubbed, and the names of the headers forwarded.

- **`record_upload_call.py`** — the original passive recorder, kept because it watches every
  page rather than one. It captures nothing unless an upload happens inside its window, which
  is why two runs wrote `start`/`watching`/`stop` and nothing else. `observe_real_upload.py`
  supersedes it for this question by producing the upload itself.

`can_we_upload.py` was **removed**. It reconstructed the request, and reconstruction is what
produced two 403s that were reported as answers. Keeping a probe that cannot answer beside one
that can is how the wrong one gets run.

### On the permission classifier

The active probes capture a credential, and the auto-mode classifier refuses them as
`[Credential Exploration]`. That is the right shape to refuse by default; it was lifted by
explicit instruction on 2026-09-18. The passive `record_upload_call.py` is **not** refused —
an earlier report that "the UploadFile verification is blocked by the classifier" conflated the
two.
