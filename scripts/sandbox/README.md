# Fresh-PC install-path check in Windows Sandbox

Evidence that the install path works on a PC that has never seen this project: a clean
Windows Sandbox, the tree from `git archive HEAD`, and only the two files a person
double-clicks.

## Rule: only what a person can do

Inside the sandbox the driver launches **only** `quickstart.bat` (setup) and
`start_all.bat` (daily start). Answers reach quickstart only as typed console input
(stdin). Nothing else in the tree is run (`bootstrap.py`, `setup_devtunnel.ps1`,
`start_all.ps1`, `doctor.ps1`, ...), no flag a person would not pass is passed, and no
`.env` / state file is pre-created. A step that needs a human (devtunnel sign-in,
M365 sign-in in a browser) is where the run stops; the transcript and screenshots show
what the person sees there. Everything else the driver does is observation: files, logs,
processes, ports, screenshots.

## Files

| File | Runs on | Does |
| --- | --- | --- |
| `run_sandbox.ps1` | host | `git archive HEAD` -> extract, `.wsb` (source read-only, results writable, networking on), start the sandbox, wait for `DONE.json`, close the sandbox |
| `sandbox_driver.ps1` | sandbox (LogonCommand) | copies the tree to `%USERPROFILE%\m365-copilot-companion`, probes the network, runs the scenario, writes evidence |
| `check_harness.py` | host | static self-check: ASCII only, parses, launches nothing but the two `.bat` files |

## Scenarios

* `AC` -- **A** `quickstart.bat` straight through on the fresh sandbox (the access
  question is answered `N`, as typed), then **C** `start_all.bat`: one click, a second
  click right after it settles, then ten clicks at the same moment.
* `B` -- `quickstart.bat` killed (process tree, like closing the window) 25 s into the
  dependency install, run again and killed the moment `.env` preparation starts, then run
  again to show it resumes and finishes the same steps (stopped once it reaches the
  devtunnel sign-in, which needs a person).

Only one Windows Sandbox can run at a time, so the scenarios are separate runs:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sandbox\run_sandbox.ps1 -Scenario AC -Work <scratch dir> -TimeoutMin 75
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sandbox\run_sandbox.ps1 -Scenario B  -Work <scratch dir> -TimeoutMin 60
```

`-Work` must be outside the repository (it receives the extracted tree and the results).

## Evidence written to `<Work>\results-<scenario>-<stamp>\`

* `progress.log` -- timeline with the first sighting of each quickstart milestone
* `<run>.transcript.txt` -- everything the quickstart console showed
* `summary.json` -- per run: exit code, elapsed, milestone timings, `state.json`
  before / at kill / after, a masked `.env` summary (key names, set/empty, 8-hex hash of
  each value, duplicates, BOM), processes alive at the kill and after it, and for
  `start_all` the new `start_all_runs.jsonl` lines, `start_all_summary.txt`, the health
  endpoint and listening ports
* `*.png` / `*.windows.txt` -- screenshots and visible window titles at each stop
* `<run>.setup_logs\` -- a copy of the sandbox's `.setup\logs`
* `DONE.json` -- completion marker

The results hold secrets minted inside the disposable sandbox (the transcript shows the
Bearer token the way quickstart shows it). Keep them out of the repository.

## Network

The sandbox gets its own NAT'd adapter from the host. It does not see the host's
loopback, so a proxy listening on the host's `127.0.0.1` is unreachable from inside;
`summary.json` -> `environment.network` records the sandbox's addresses, routes,
WinHTTP / WinINet proxy settings and a HEAD probe of every download host the install
uses, so a failure can be told apart from "this network blocks it".
