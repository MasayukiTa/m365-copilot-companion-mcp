## 2026-11-11 - the bridge refuses an agent URL with no id, with the shared sentence (PR #581)

- Result. A configured agent URL that carries no agent id no longer silently reaches
  the default Copilot. The address parser already resolved every form the browser shows
  (`/chat/agent/T_<guid>.<guid>` on both hosts, with or without a query string, and the
  `/agents/copilotstudio/T_<guid>.<guid>` redirect form) and refused the no-id forms (a
  generic chat page, a one-guid link) with `NO_AGENT_ID_MESSAGE`. configure_env,
  quickstart option R and the doctor already printed that one sentence. This slice wires
  the same sentence into the one place that still let a bad address through: the bridge
  startup, which opened a tab on whatever URL it was given. Now it validates the URL
  first and exits with the shared sentence before any tab is opened. Shipped on the
  existing PR #581 branch (`fix/agent-url-forms`); after push `mergeable` is MERGEABLE and
  the fast CI checks (identities, crlf-python, Analyze python/csharp, actionlint,
  psscriptanalyzer, gitleaks, CodeQL, Release ZIP smoke) are green; the long `test` and
  `windows-install-smoke` jobs were still running when this entry was written.

- Change. `bridge/copilot_bridge.py` gains `validate_impl_agent_url(url)`: an empty value
  is left alone (startup keeps its existing fallback to any open agent tab), and a
  non-empty value is passed through the same `relay.template_seed.agent_id_from_url`
  resolver the rest of the surface uses -- if it yields no id, the bridge exits with
  `NO_AGENT_ID_MESSAGE` instead of opening a tab on it. It is called on the startup path
  where the configured URL is first read. No new message string is introduced; the
  sentence is imported from the one definition so the four surfaces cannot drift.

- Behaviour. Only the no-id case changes. Every address form the browser actually shows
  still resolves and still opens its tab; an empty configuration still falls back exactly
  as before. The new refusal happens before a tab is opened, so a misconfigured URL fails
  loudly at startup rather than looking like it worked while talking to the wrong
  assistant.

- Tests. `bridge/test_agent_url_seed_surface.py` (new) pins it: each valid form and the
  empty value pass through `validate_impl_agent_url` without raising, and each no-id form
  exits with the shared sentence. Ran on a clean interpreter (the stack's own venv had a
  broken pytest, so a separate system Python 3.10 with pytest 9.1.1 was used, basetemp
  kept inside the clone): `relay/test_agent_url_forms.py` 23 passed,
  `relay/test_template_seed.py` 5 passed, `bridge/test_agent_url_seed_surface.py` 10
  passed. Both the new test and `relay/test_agent_url_forms.py` are registered in the
  ci.yml hermetic list; `scripts/check_ci_test_manifest.py` reports OK.

- Discussion. The branch as found had removed `relay/test_agent_url_forms.py` from ci.yml
  on the belief that the task forbade touching the manifest, but the repo's own manifest
  audit fails a tracked test that is neither listed nor explicitly EXCLUDED -- so that
  removal left the audit red. The enforced gate is the real rule, so both tracked test
  files are listed; the audit now passes. The fix lives next to the existing parser and
  reuses its resolver and its message, so no second source of truth and no second copy of
  the sentence is introduced.

- Open. The long `test` and `windows-install-smoke` CI jobs were in progress at write
  time; if either comes back red it is addressed on the same branch before merge. The
  fresh clone under the agent's TEMP working tree is removed after this entry is pushed.
