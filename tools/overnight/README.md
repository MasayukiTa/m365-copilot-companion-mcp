# Overnight investigation batch

Queue a list of short requests before going to bed; in the morning there is one report.

The module never queries a database itself. It hands each request to the fleet, reads the
fleet's own state files to see what finished, and compiles the answers.

## Submit route

`--submit-route queue|gui` (default `queue`, meant for the night).

- `queue`: each goal is written to the fleet's pending queue through `fleet_submit`
  (`tools/fleet_intake.py`). It does not need the screen, so a locked desktop does not stop it.
  The door's own rules apply: a refusal as a near-duplicate (word overlap 0.5) is reworded once and
  retried; requests that read like one still in flight are held back until it finishes.
- `gui`: the way a person does it, `scripts/win/submit_via_ui.ps1`, one call per item (the daytime
  route). It types into the cockpit window, so it needs an unlocked interactive desktop and has no
  lock detection: on a locked screen it fails (after retries), and every item ends up as
  `投入不可(画面ロック等)`. Do not use it for an unattended night unless the screen stays unlocked.

Neither route changes any setting or power/screen-lock behaviour.

## Use (one command at night)

```
python -m tools.overnight run --items items.txt --start-at 02:00 --deadline 07:00
```

Options: `--out <folder>` (default `Documents\朝レポート\<YYYYMMDD_HHMM>`),
`--max-minutes-per-item 20`, `--max-items 50`, `--max-inflight 4`, `--fleet-dir <dir>`
(default `<repo>/.fleet`; run it from the checkout the fleet runs from).
All times are JST. While waiting and running, Windows is asked not to sleep.

## Input

`items.txt` (UTF-8): one short Japanese request per line, optional `A1: ` id prefix.
`# 前置き: ...` is one sentence added to every request. Blank lines and other `#` lines are ignored.

```
# 前置き: 数値は小数1桁で
過去1か月の不良データを集計して
A2: 品種ごとの月別推移を出して
```

`items.json`: `[{"id": "A1", "ask": "...", "tags": [], "max_minutes": 20}]`
(or `{"preamble": "...", "items": [...]}`). Duplicate ids/requests are skipped and listed; items
over the limit are not queued and are listed.

Each request becomes a goal of at most two sentences:
`[A1] <request>。結果の表と図は <folder>/A1/ に保存して、要点を3行で答えて。`
The `[A1]` tag is how results are matched back.

## Output (in the run folder)

- `朝レポート_<date>.xlsx` - sheets: サマリ (one row per item, links to artifacts and to the item
  sheet, an `OK/NG` column for the reader), 失敗と未完 (cause and what to do next), 実行情報,
  and one sheet per item (key points, SQL if the answer contained it, interpretation notes,
  imported table with a native Excel chart, PNG images).
- `朝レポート_<date>.md` and `.html` (self-contained) with the same content.
- `READY.txt` (written last), `state.json`, `overnight.log`, `report_error.txt` (only if a
  report step failed; a plain md report is still written).
- A Windows toast when done (ignored if it fails).

A report is always produced, even if nothing succeeded.

## Resume

`python -m tools.overnight resume --out <folder> [--deadline 07:00] [--retry-failed]`

`state.json` is written atomically after every change. An item already handed to the fleet
(same id, same goal text) is never sent again; an item that was half-submitted when the process
died is looked up in the fleet first. `--retry-failed` queues failed / timed-out / skipped
items again (with reworded text, to get past the fleet's duplicate rule).

## Time budget

`--max-minutes-per-item` (or `max_minutes` per item in json) counts from when the worker
started. Past it, the item is recorded as `timeout`; the worker is NOT stopped (the fleet's own
mechanisms decide that). `--deadline`: no new items after it; running ones are not waited for;
the report is built from what exists. Free disk close to the owner's `disk_floor_gb` setting
(read, never written) also stops queuing.

## Failure classes

Decided from the end of the worker's answer: `DB接続不可`, `ロック(解錠不可)`, `タイムアウト`,
`依頼が曖昧で保留`, `その他`; plus `投入不可(画面ロック等)` for a request that never reached a worker.
When any item could not be submitted, the report starts with a banner such as
"N 件中 M 件が投入できませんでした。原因候補: ..." (xlsx summary sheet, md, html), and the run is
not reported as `finished` when no item succeeded. A "done" answer whose ending looks like a connection/lock failure is
flagged for review rather than silently counted as a success.

## Morning consistency check

At the end of the run the report gets a mandatory section/sheet `整合検査`: the cockpit's own
view (`submit_via_ui.ps1 -ReadOnly`, a dry run; if it cannot be read, e.g. locked screen, the reason
is written) and a comparison of the fleet's `status.json` worker list with the report's items
(for example "report says time-out, worker has since finished"). Every gap is one sentence.

## Feedback

Put ○ or × in the `OK/NG` column. `python -m tools.overnight feedback --report <xlsx>` (and the
start of the next `run`) appends the marks, with item id and SQL, to `feedback.jsonl` in the
parent of the run folder.

## Safety

Read-only toward the fleet state; no database access; no process is ever killed; goals go in
only through the queue door (night) or the cockpit GUI route (day); settings are never edited;
nothing needs an approval dialog.

## Limits

- Matching depends on the `[ID]` tag appearing in the fleet's goal text; fan-out children whose
  goals lose the tag are only seen through their parent.
- Completion comes from the fleet's status snapshot and transcripts; if the snapshot is
  replaced by another run, an item falls back to its transcript (considered finished after
  15 idle minutes with an answer).
- The `gui` route needs an interactive desktop session (screen unlocked); the consistency check's
  `-ReadOnly` call needs one too, and says so in the report when it cannot run.
- Charts are simple column charts over the first numeric column of the worker's table.

Japanese one-page guide: USAGE_ja.md
