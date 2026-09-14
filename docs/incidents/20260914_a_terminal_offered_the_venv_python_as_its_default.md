# A terminal offered the project's venv python as its default shell

**2026-09-14.** Reported by the operator with a screenshot: a terminal application's new-tab
dropdown reads

    Default: C:\Users\<user>\<repo>\.venv\Scripts\python.exe
    ctrl+alt+1

so opening a terminal lands in a Python REPL instead of a shell. The operator states this
happened about a week earlier, was claimed fixed, and has now recurred.

## The first thing to record is that there was nothing to read

**No record of the earlier occurrence exists** — not in `docs/incidents/`, not in the memory
store, not in any commit. Whatever was done a week ago left no trace, so this investigation
started from zero instead of from a known boundary. That absence is the reason the same hours
were spent twice, and it is the reason this file exists even though the cause was NOT found.

## 特定できなかった

No setting on this machine, at the time of the search, names the venv python as a terminal
profile or shell. The search was exhaustive and is listed so the next occurrence does not repeat
it.

| looked at | result |
|---|---|
| Windows Terminal `LocalState\settings.json` | no `venv` / `python.exe`; last modified 2026-07-16 |
| Windows Terminal fragments | only `Git\git-bash.json` and one `Microsoft.WSL` entry |
| Windows Terminal Preview / unpackaged build | not installed |
| VS Code global settings | **no `terminal.integrated.*` key at all** |
| VS Code profiles / workspace files / `.vscode\settings.json` (3 found) | one `python.defaultInterpreterPath`, a different project's venv, not a terminal profile |
| Claude desktop app, every settings file | no match for `venv` / `python.exe` / `defaultProfile`; its `terminalCliPowerShellPath` is an ordinary `powershell.exe` |
| Tabby / Hyper / ConEmu / WezTerm / Fluent / Alacritty | not installed; absent from the registry uninstall lists |
| JetBrains, Visual Studio | not installed |
| `%APPDATA%` + `%LOCALAPPDATA%`, recursive string search for the venv python path | **zero matches**, unfiltered and not truncated |
| repository `scripts/` and setup files | no settings writes, no `code --install-extension`, no profile manipulation |
| scheduled tasks | one task runs the venv python directly (`SelfImproveNightly`), and writes no settings |

## The one state that mentions it, and why it is not the answer

VS Code's workspace state database
(`...\Code\User\workspaceStorage\<hash>\state.vscdb`, last modified 2026-08-18) holds

    ms-python.vscode-python-envs:venv:WORKSPACE_SELECTED
      {"c:\\Users\\<user>\\<repo>": "c:\\Users\\<user>\\<repo>\\.venv\\Scripts\\python.exe"}

That is the extension recording which interpreter is selected. Its own code was read: the
strings `defaultProfile` and `registerTerminalProfileProvider` do not appear anywhere in it, its
`createTerminal` call passes `env` and `name` and never `shellPath`, and the workspace's terminal
history shows an ordinary `pwsh` session running `Activate.ps1`. Selecting an interpreter and
making python.exe the shell are different things, and nothing found does the second.

## What would settle it

Which application's dropdown that was. Every candidate whose configuration lives on disk has
been read and none carries it, which leaves the possibility that the value is **derived at
runtime** — an application detecting a virtualenv in the working directory and offering it as a
profile without persisting anything. That would fit every observation: no file to find, recurs
when the venv is present, and appears "fixed" whenever the detection does not fire.

Until the application is known, **assume it will recur.** Nothing was changed by this
investigation.
