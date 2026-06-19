<!-- Thanks for contributing! Keep the description focused; link any related issue with "Fixes #NN". -->

## Summary

<!-- What does this PR change, and why? -->

## Type of change

- [ ] Bug fix
- [ ] New tool / feature
- [ ] Setup / bootstrap
- [ ] Docs
- [ ] Refactor / chore

## How I tested

<!-- Commands run, manual steps, clean-env check, screenshots. -->

## Checklist

- [ ] No secrets committed: `.env` stays out of git; no API key, unlock password, tenant GUIDs, or tunnel tokens in code or docs.
- [ ] `.bat` files are ASCII / English only (cmd mis-decodes non-ASCII). Any `.ps1` containing Japanese keeps its UTF-8 **BOM**, and files read by Python/plain parsers (e.g. `.env`) are written **without** a BOM.
- [ ] Any new third-party `import` was added to `requirements.txt`.
- [ ] The server still imports where relevant: `python -c "import main"` succeeds (and `list_my_tools` looks right).
- [ ] Commit messages are in English.
