# m365-copilot-companion-mcp

> Microsoft 365 Copilot shipped you a brain. It forgot the hands.
> This is the hands.

A personal-use **Model Context Protocol (MCP) server** that turns one
laptop into a fully-capable agent backend for **Microsoft 365 Copilot**,
**Claude Desktop**, or any other MCP-aware client. About 100 tools. Zero
external API keys. Built in roughly a day. Used in anger ever since.

The motivating frustration: corporate M365 Copilot licences come with
Claude Opus included, but Opus has no fingers. It can read what you paste
into the chat, and that's about it. This server gives the model **real
hands** on the one laptop where you have permission to do as you please —
your own.

```
[ M365 Copilot ]  ──▶  [ Copilot Studio agent ]  ──▶  [ Dev Tunnel ]
                                                            ↓
                                     [ m365-copilot-companion-mcp on your laptop ]
                                                            ↓
                                              your files · Python · DBs · web
```

One user, one companion, one laptop. Nothing is centralised. Nothing
leaves the box you wouldn't want it to. The whole thing costs **zero**
beyond the M365 Copilot licence you already have.

> Not affiliated with, endorsed by, or sponsored by Microsoft Corporation.
> "Microsoft 365", "Copilot", and "Copilot Studio" are trademarks of their
> respective owners; they are referenced here only to describe what this
> companion attaches to.

---

## ⚠️ Before you go any further

This is a thing you run **on a machine you control**, against accounts and
data **you already have permission to touch**, for **your own use**. It is
not a SaaS, it is not a hosted service, there is no support contract, and
the friendly Microsoft-flavoured name in the repo does not make any of
that less true.

If your employer:

- blocks personal MCP servers,
- has not approved Microsoft Dev Tunnels for use,
- has a policy against running Copilot Studio agents outside an
  IT-managed template,
- forbids installing arbitrary Python packages on company hardware,
- considers any of: AI tool execution, agent file access, or third-party
  GitHub clones a security incident,

…then **do not deploy this on a company laptop**. Read it for ideas,
build your own equivalent through proper channels, but don't paste the
Bearer key into your work Copilot Studio and hope nobody notices.

The licence is MIT. There is no warranty, no obligation, no liability.
**Whatever you blow up on your side is your problem.** Move carefully,
especially with the unlock password.

OK. With that out of the way:

---

## 🎯 What this thing can do

`main.py` registers a single flat catalog of tools. Call `list_my_tools`
at runtime to see all of them. Highlights:

| Category | Tools | What it's for |
|---|---|---|
| **Code execution** | `run_python`, `shell_exec`, `run_python_in_background`, `run_in_background`, `job_wait`, `job_status`, `job_output`, `job_list`, `job_kill` | Run code. Wait for results. Kill the runaway. |
| **File I/O** | `read_file`, `write_file`, `append_file`, `list_directory`, `glob`, `find_files`, `copy_path`, `move_path`, `trash_path`, `create_directory`, `delete_path` | Read, write, move, delete inside the allowed base. |
| **File forensics** | `hash_file`, `find_duplicates`, `dir_size`, `file_metadata` | "Where did 80 GB go?" — answered in one prompt. |
| **Editing / search** | `grep`, `replace_in_file`, `multi_edit`, `diff_files`, `python_check` | Atomic multi-edit. No more "the agent ate half my file." |
| **Git** | `git_status`, `git_diff`, `git_log`, `git_branch`, `git_blame`, `git_add`, `git_commit`, `git_checkout` | Reads and writes. `git_blame` finds the colleague to gently roast. |
| **Tabular / JSON** | `read_excel`, `write_excel`, `summarize_table`, `read_json`, `write_json` | First-class spreadsheet handling. |
| **PDF / OCR** | `read_pdf`, `pdf_info`, `ocr_image`, `ocr_pdf` | Digital and scanned. |
| **Image (self-verify)** | `read_image`, `image_info` | The agent can finally see what it just made. |
| **PowerPoint** | `create_pptx`, `pptx_from_markdown`, `pptx_info`, `pptx_add_slide`, `pptx_add_image`, `pptx_add_table`, `pptx_replace_image`, `pptx_export_png` | Generate decks, embed real charts, then render each slide back to PNG to audit. |
| **Diagrams / math** | `render_diagram` (mermaid / graphviz / plantuml / d2 via [Kroki](https://kroki.io)), `render_mermaid_png`, `render_math` (matplotlib mathtext, no LaTeX install needed) | Architecture diagrams and equations on demand. |
| **Web** | `web_fetch`, `web_search`, `web_search_news`, `github_file` | DuckDuckGo search, URL fetching, raw GitHub file pulls. |
| **Databases** | `sqlite_*` and `odbc_*` (six each) | Read-only SQL over Windows / Entra integrated auth. The companion inherits the user's existing DB privileges; no DBA involvement required. |
| **Persistent memory** | `memory_save`, `memory_load`, `memory_list`, `memory_delete` | Cross-session notes the agent remembers next week. |
| **Scheduling / watching** | `schedule_*` (Windows Task Scheduler), `watcher_*` (filesystem watchdog) | Time-axis autonomy. "Every Friday at 9, regenerate the weekly report." |
| **Archives** | `zip_list`, `zip_extract`, `zip_create` | With zip-slip protection. |
| **Notifications** | `notify_desktop` | Windows toast. Pair with `job_wait` for "ping me when the model run finishes." |
| **Environment** | `env_info`, `pip_install`, `which`, `list_my_tools` | Introspect the host; install missing packages on the fly. |
| **Security** | `unlock`, `list_unlocked` | Per-IP password unlock for mutating tools. |
| **Misc** | `todo_write`, `todo_list`, `todo_clear` | A scratchpad for the agent's own planning. |

Every tool ships with a clear docstring; the agent reads them to pick
the right one. To add a new ability, write a Python function with a
good docstring, add it to the `TOOLS` tuple in `main.py`, restart. That
is the entire extension story.

---

## 🚀 Setup

### 0. Prereqs

- **Windows 10 or 11** (PowerShell 5+). Most things work cross-platform;
  `schedule_*` and `notify_desktop` are Windows-only.
- **Python 3.10+** (3.11 recommended).
- **Git**.
- Optional but useful:
  - **Tesseract OCR** + the language pack you need
    ([UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki))
  - **Poppler** on PATH if you want `ocr_pdf`
  - **ODBC Driver 18 for SQL Server** if you'll query corporate DBs over
    Entra

### 1. Clone & virtualenv

```powershell
git clone https://github.com/<your-account>/m365-copilot-companion-mcp.git
cd m365-copilot-companion-mcp

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Create `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

Generate fresh secrets — **per user, never share, never commit**:

```powershell
python -c "import secrets; print('MCP_API_KEY=' + secrets.token_hex(20))"
python -c "import secrets; print('MCP_UNLOCK_PASSWORD=' + secrets.token_hex(8))"
```

Paste into `.env`. Tighten `MCP_ALLOWED_BASE` to a sub-folder if you want
the companion to live in a smaller pen.

### 3. Start the server

```powershell
.\start.ps1
```

The MCP server listens on `http://127.0.0.1:8000/mcp` (Streamable HTTP).

### 4. Expose it (only if a remote client needs to reach it)

For local Claude Desktop on the same laptop, skip this step entirely.

For Microsoft 365 Copilot Studio you need an HTTPS URL pointing at
`localhost:8000`. The simplest free option is
[Microsoft Dev Tunnels](https://learn.microsoft.com/azure/developer/dev-tunnels/):

```powershell
winget install Microsoft.devtunnel
devtunnel user login                          # first time only
devtunnel create m365-copilot-companion --allow-anonymous
devtunnel port create m365-copilot-companion -p 8000 --protocol http
devtunnel host m365-copilot-companion
# → https://<random>-8000.<region>.devtunnels.ms
```

`--allow-anonymous` is safe **because** this server enforces a Bearer API
key and a per-IP unlock layer on top of the tunnel. Anyone who somehow
guesses the random tunnel URL gets a 401 immediately.

### 5. Register with your MCP client

**Microsoft 365 Copilot Studio:**

1. Create / open your Copilot Studio agent.
2. Tools → *Add a tool* → *Model Context Protocol*.
3. Server URL: `https://<your-tunnel>-8000.<region>.devtunnels.ms/mcp`.
4. Authentication: *API key (manual)*. Header name `Authorization`,
   value `Bearer <your MCP_API_KEY>`.
5. Save → *Add connection* → about 100 tools should appear.

Publish via the "Microsoft 365 + Teams" channel, scoped to **yourself
only**, until you have made peace with what the agent can now reach.

**Claude Desktop / Claude Code:**

```json
{
  "mcpServers": {
    "companion": {
      "transport": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <your MCP_API_KEY>" }
    }
  }
}
```

### 6. Smoke test

Ask the agent:

> "Call `list_my_tools`."

You should see roughly 100. Read-only tools just work. The first time
you try something that writes or executes, you'll be told:

> `[locked client IP: '203.0.113.42'] Call unlock(password='...') first.`

Do as you're told. The IP is then trusted for `MCP_UNLOCK_TTL_DAYS`
(30 by default). Subsequent mutations are silent.

---

## 🔐 Security model

Three layers stacked in order:

| Layer | Mechanism | Failure mode |
|---|---|---|
| **Authentication** | Static Bearer token (`MCP_API_KEY`, 40 random hex chars) | 401 Unauthorized |
| **Filesystem authorisation** | Every path goes through `_validate_path`, which rejects anything outside `MCP_ALLOWED_BASE` | `PermissionError` |
| **Mutation authorisation** | Write / execute tools call `require_unlocked()`, which checks the caller IP (from `X-Forwarded-For` or socket peer) against an unlock list with TTL | Returns a polite "locked" message instructing the caller to run `unlock(password)` |

Worth knowing:

- The unlock password is **separate** from the API key. Either alone is
  not enough to mutate.
- `127.0.0.1` / `::1` are implicitly trusted (local dev).
- ODBC queries are opened `readonly=True` and verb-allowlisted to
  `SELECT / WITH / EXEC / SHOW / DESCRIBE`. Auth is whatever Windows or
  Entra hands the running user. No new DB account, no new password.
- Rotate the Bearer key by editing `.env` and restarting. Old keys die
  immediately.

What this is **not**:

- It is not a hardened multi-tenant service.
- It is not pen-tested.
- It does not protect you from a local attacker who has shell on your
  laptop — that attacker already has everything the agent has.
- It does not protect your organisation from you handing the unlock
  password to a coworker. Don't do that.

---

## 🛟 The killer feature: self-verification

Most agent failures happen because the model claims a task is done but
never actually checks. This server gives the agent two tools to inspect
its own output:

- `read_image(path)` returns a base64 data URI consumable by
  vision-capable models. Generate a chart with `run_python` (matplotlib),
  then ask the agent to read it back and confirm the axes are labelled.
- `pptx_export_png(pptx_path)` drives PowerPoint over COM to render
  every slide to PNG. Use this after `create_pptx` to confirm images
  embedded correctly and Japanese characters didn't tofu.

A typical "trust but verify" loop:

```text
run_python       →  saves chart.png
read_image       →  agent inspects, fixes mistakes
create_pptx      →  embeds chart.png into report.pptx
pptx_export_png  →  agent skims each slide PNG
notify_desktop   →  "Report ready: report.pptx"
```

Wire this loop into the system prompt of your Copilot Studio agent and
the "phantom PowerPoint with no charts" failure mode disappears.

---

## 📁 Repository layout

```
m365-copilot-companion-mcp/
├── main.py                  # FastMCP entry point and tool registry
├── start.ps1                # Convenience launcher (uses .venv if present)
├── requirements.txt
├── .env.example             # copy to .env and fill in
├── .gitignore               # excludes secrets, runtime state, business data
├── LICENSE                  # MIT
│
├── tools/
│   ├── code_exec.py         # run_python, shell_exec
│   ├── jobs.py              # background job manager
│   ├── file_ops.py          # filesystem I/O + forensics
│   ├── search_ops.py        # glob, find_files
│   ├── archive_ops.py       # zip handling
│   ├── coding_ops.py        # grep, multi_edit, git_*, diff_files, python_check
│   ├── data_ops.py          # Excel / CSV / JSON
│   ├── pdf_ops.py           # PDF text extraction
│   ├── ocr_ops.py           # Tesseract wrappers
│   ├── image_ops.py         # read_image, image_info (self-verification)
│   ├── pptx_ops.py          # PowerPoint generation + COM export
│   ├── diagram_ops.py       # Kroki-backed diagram renderer
│   ├── render_ops.py        # matplotlib mathtext math renderer
│   ├── web_ops.py           # web_fetch, github_file
│   ├── search_web.py        # DuckDuckGo search
│   ├── sql_ops.py           # SQLite (read-only)
│   ├── odbc_ops.py          # ODBC / SQL Server / Azure SQL
│   ├── schedule_ops.py      # Windows Task Scheduler wrapper
│   ├── watcher_ops.py       # Folder watcher (via watchdog)
│   ├── memory_ops.py        # Cross-session memory store
│   ├── notify_ops.py        # Windows toast notifications
│   ├── env_ops.py           # env_info, pip_install, which
│   ├── task_ops.py          # todo_write / list / clear
│   ├── registry.py          # @register decorator + list_my_tools
│   └── security.py          # unlock / require_unlocked / IP allowlist
│
└── agent_memory/            # long-term notes (mostly git-ignored)
    ├── README.md            # tracked: schema description
    └── templates/           # tracked: blank templates
```

---

## 🧩 Suggested system-prompt fragment

Paste into your Copilot Studio (or Claude Desktop) agent instructions:

```
You are the operator of the companion. The companion lives on the user's
PC and exposes a wide set of MCP tools.

- When unsure what is available, call list_my_tools first.
- Read-only tools are always available. Mutating / executing tools
  require unlock(password) per IP; surface the error to the user once
  if it happens.
- Save outputs under ~/Desktop/<task-name>/ by default.
- Right after generating an image or slide deck, call read_image or
  pptx_export_png and self-verify. If something is missing or tofu'd,
  fix it and re-export — silently is fine, up to three iterations.
- For heavy or slow work, prefer run_python_in_background + job_wait
  and notify_desktop when done. Don't make the user stare at a spinner.
- When you learn something durable about the user or a project,
  memory_save it. Read with memory_load / memory_list before answering
  about previously discussed topics.
- Never send confidential data to external services (Kroki, web_fetch, etc.).
  When in doubt, do it locally.
```

---

## 🛠 Troubleshooting

| Symptom | Fix |
|---|---|
| `pyodbc` can't connect | Install ODBC Driver 18 for SQL Server; verify with `odbc_drivers` |
| OCR returns nothing | Install Tesseract + the language data, then run `which("tesseract")` |
| `pptx_export_png` fails | Microsoft PowerPoint must be installed on the host (it drives PowerPoint over COM) |
| `render_diagram` SSL errors | Corporate proxy blocking Kroki. Either get the CA right or skip the tool and let the agent draw locally with matplotlib |
| Copilot Studio request times out | Each Copilot Studio call has ~90 s budget. Split with `run_in_background` → `job_wait` |
| `unlock` keeps being requested | Caller IP changed (VPN switch, Copilot Studio backend hop). Unlock again. |

---

## 🤝 Contributing

PRs and issues welcome. Please:

- Keep tool docstrings short and concrete — the LLM reads them, not just
  humans.
- Wrap any mutating tool in `require_unlocked()` from
  `tools/security.py`.
- Route any disk access through `_validate_path` so it stays inside
  `MCP_ALLOWED_BASE`.
- **Never** commit `.env`, `.unlock_state.json`, `.memory_state.json`,
  or business data. The `.gitignore` is set up for this; please don't
  loosen it.
- If a new tool calls an external service, document what data leaves
  the device and let the operator opt out.

## 📜 License

[MIT](./LICENSE). Use freely, at your own risk, with the warning above
firmly in mind.
