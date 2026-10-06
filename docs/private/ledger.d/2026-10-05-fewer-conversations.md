## 2026-10-05 - Fewer Copilot conversations: lazy ids, an unsent row, and `merge_conversation`

- Problem. The conversation-count analysis found two avoidable kinds: (A) conversation GUIDs with
  no message (467 in one burst on 9/30, workers w0-w483; 48 zero-turn worker transcripts on 10/4
  carrying an id before the first message), and (B) one FRESH aggregator conversation per fan-out
  family (422 on 10/4, about a quarter of the volume) beside the splitter and the N children.
- Where Part A came from (read from the code; the burst itself was not replayed). NOT from the DOM driver: a fresh Copilot tab creates its conversation
  when the first message is submitted (`copilot_autopilot_relay.send`, whose single-submit
  ambiguity guard `FreshSubmitAmbiguous` is untouched). The socket route minted the id in
  `chathub.Conversation.__init__`, and `RelayWorker._capture_url` (every poll) wrote it to
  `conv_url`, the transcript `guid` line and, at close, `worker_done.conv_client` -- for a worker
  that never sent. Those rows are what read as 467 conversations. The backend never saw them.
- Change A. `relay/chathub.py`: the id is minted by the first read that puts it on the wire
  (`url_for_turn` / `chat_frames`, i.e. the same call that sends the first message);
  `peek_conversation_id()` reads without creating. `socket_driver.conversation_ids()` peeks, so
  a silent conversation records an empty id everywhere (resume lookups and the reconnect path no
  longer chase an id the backend never created). A resumed id is set up front as before.
  `relay/socket_route.py` (frozen) is read-only here and needed no change: it only assigns the
  resume id. Nothing could be made lazier at a DOM site: the tab path already creates at submit.
- Change A2. `relay/conversation_saving.py` + `RelayWorker`: `_attached_ts` is set when attach
  opens a conversation/tab; `conversation_created_unsent` (mechanism row, once per worker) is
  written at close or at the send-pacing wait when 60 s passed with nothing sent. Fields: route,
  age_s, where, has_conversation_id. Workers that send at once never match (`_t_send`/`turn`).
- Change B. Setting `merge_conversation` = `fresh` (default, behaviour unchanged, golden digest
  test still passes) | `parent`. GUI: cockpit gear popup, Fan-out section, "Merge conversation /
  統合を実行する会話", SaveKey only, "in effect" from additive status.json
  `conversation_saving {merge_conversation, aggregators_saved, unsent_created}`
  (`fleet_runner._snapshot`). EACH_GATE: re-read at each split and each merge.
- Feasibility of `parent`. The parent WORKER and its slot end at the split by design (that
  prevents the slot deadlock, see `fanout.aggregation_goal`), but its CONVERSATION is not gone:
  a socket conversation continues by id (measured 2026-08-24; `driver_for(conversation_id=)`).
  So `parent` changes only the transport: the merge goal item gets `resume_conv=<parent id>`, the
  existing follow-up mechanism. The merge remains its own goal with the same campaign id, task
  id, role, depth, checks, admission slot and `goal_resume_key`, so the one-merge-exactly-once
  flag (`_camp["merged"]`), `merge_requeued`, the nested-slot rows (`_note_nested_result`) and the
  resume rebuild are not involved (tests pin: one merge, one `merged` line, restart issues none).
  Not available, falls back to fresh: a tab parent, a parent that never spoke, a family adopted
  from the ledger after a restart (the id is memory-only on purpose).
- Why the default stays `fresh`. Structure is safe; ANSWER QUALITY is not measured: the parent's
  conversation was primed to emit a split plan and may answer the merge prompt with another one.
  The `aggregator_conversation` row (slices, input chars, `concat_candidate`) is written for every
  merge so a small A/B can be read. The cheaper levers do not apply: `MIN_CHILDREN` is 2, so n==1
  never occurs, and no family has all slices from one worker; `concat_candidate` is the
  measurement of whether a deterministic join would have sufficed.
- Tests. `relay/test_conversation_saving.py` (lazy id, peek, wire-time minting, unsent row cases,
  setting reader, cockpit source shape, merge applied/not applied, `run_relay_fleet` with fake
  split parents: fresh unchanged, parent once + restart, tab/adopted fall back). Settings test
  registry updated. `bench/ui_build_check` builds both exes clean.
- Open. No live A/B of merge quality yet; expected saving if `parent` were on for every socket
  family is up to one conversation per family (about 400 per day at the 10/4 volume).
