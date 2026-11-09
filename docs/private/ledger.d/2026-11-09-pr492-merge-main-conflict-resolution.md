## 2026-11-09 - PR #492 merge origin/main, resolve conflicts keeping both sides' intent (dirty-PR batch, slice 5/12)

- Scope. One slice of the dirty-PR batch for MasayukiTa/m365-copilot-companion-mcp: PR #492 only (feat/periodic-scan-generic, base main, was CONFLICTING). Fresh clone under a temporary isolated working directory. Other PRs in the batch handled by sibling conversations; not touched here.

- Merge. git merge origin/main (origin/main at 17833480) into feat/periodic-scan-generic (was 7db62a22). Two content conflicts, both resolved keeping BOTH sides' intent:
  - .github/workflows/ci.yml: the hermetic pytest list tail. HEAD added scripts/periodic_scan/tests/test_runner.py; origin/main added tools/test_a_bash_assignment_does_not_reach_shellexecute.py. Both kept, plus the shared relay/test_a_stale_coordinator_restarts_only_when_idle.py line. Final tail (ci.yml 1177-1179): test_a_bash_assignment... \ , test_a_stale_coordinator... \ , periodic_scan/tests/test_runner.py (no trailing backslash, ends the arg list).
  - docs/CONFIG.md: HEAD added the "定期スキャン (scripts/periodic_scan) のローカル設定" section; origin/main added the "データソース語彙 (.config/domain_terms.json)" section. Both are independent doc sections -- kept both, separated by a --- rule, markers removed.

- Business-specific names. None introduced. The PR's additions use only generic framework names (periodic_scan, scan_profile, domain_terms) and documentation placeholders explicitly labelled 架空/偽 in CONFIG.md. Checked the added/changed text against the project's forbidden-terms list used for public-repo hygiene: no hit (every site-specific product/process/column/system token and the organization-name pattern in that list are all absent). Nothing needed to move into .config/domain_terms.json; the PR already keeps code generic and site values in the gitignored .config/ profile.

- EOL. Both conflict files are attr/text eol=lf; after git add the index side is i/lf, so the CI "Reject CRLF in tracked ... YAML/Markdown" gate is satisfied even though the working copy is CRLF.

- Tests. Touched tests = scripts/periodic_scan/tests/test_runner.py (the file this PR adds). Local run in the merged tree: 7 passed in 69.6s. Conflict-marker scan clean (git diff --check clean). test_runner.py confirmed present in the ci.yml hermetic manifest (line 1179), so check_ci_test_manifest.py registration is satisfied.

- Delivery. Merge commit 3e0b273a (Merge remote-tracking branch 'origin/main' into feat/periodic-scan-generic). Pushed to origin feat/periodic-scan-generic (7db62a22..3e0b273a). main NOT touched. PR #492 should now be mergeable once CI is green.

- CI at handoff. gh pr checks 492: Release ZIP smoke pass; test / Analyze (python,csharp) / C# compile / actionlint / gitleaks / setup.bat (crlf-python, lf-uv) / windows-install-smoke / CodeQL still pending (runner queue). No failures observed. Final verdict to be read from the completing run.
