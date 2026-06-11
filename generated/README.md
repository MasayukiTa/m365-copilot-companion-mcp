# generated/

Files in this directory are produced by `scripts/bootstrap.py` (run via
`setup.bat`). They are environment-specific helpers, not source.

- `copilot-connector.md` -- step-by-step checklist for adding this MCP server to
  a Copilot Studio agent (tunnel URL + Bearer key from `.env`). Written by the
  `gen_connector` step. It is regenerated on each run and embeds your real
  `MCP_API_KEY`, so treat the generated copy as a secret and do not commit it.

The committed `copilot-connector.template.md` is the same checklist with the key
redacted, kept for reference.
