# Ledger entries (one file per entry)

The work ledger `../claude_work_ledger.md` is append-only and is now FROZEN as the baseline:
do not append to it any more. A new entry is its own file here, so concurrent PRs never
touch the same lines and GitHub's merge cannot report a conflict.

How to write an entry:

1. Create `docs/private/ledger.d/<YYYY-MM-DD>-<slice>.md` (lowercase, one file per slice).
2. Start it with `## <YYYY-MM-DD> - <title>`, then the same bullets as before (Change,
   Behaviour, Tests, Open). End the file with a newline.
3. To read the whole ledger: `python scripts/ledger_compile.py` (stdout) or `--out FILE`.
   Do not commit the compiled output; that would bring the shared file back.
4. CI runs `python scripts/ledger_compile.py --check`.

`.gitattributes` also marks the baseline `merge=union`, which only helps local merges; the
per-entry files are what work server-side. Same rules as the ledger: no secrets, no company
names, no employee IDs, no user-name paths.
