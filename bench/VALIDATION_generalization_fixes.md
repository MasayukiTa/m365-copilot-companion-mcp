# Generalization fixes for the 3 holdout non-resolved instances

Goal: address the failure **classes** the 3 non-resolved holdout instances (sphinx-7738,
requests-2148, requests-2317) exposed — via **domain-general** scaffold/harness improvements,
NOT instance-specific patches. The debugged instances stay **burned** (excluded from any score
claim); these changes are validated on synthetic fixtures + general TLS checks, and the holdout
is left sealed (no re-eval of the burned/holdout instances to "prove" a lift — that would be
circular overfitting).

The 3 instances collapse to **2 failure classes** (the suspected "pytest staleness" 3rd class was
a red herring: requests-2317's `pytest.raises(ValueError, "string")` TypeError is not in its
FAIL_TO_PASS/PASS_TO_PASS sets, so it is not a counted failure).

---

## Class 1 — "fixes the symptom but drops a config/branch behavior" (sphinx-7738)

**Symptom.** Model fixed the target bug (FAIL_TO_PASS passed) but broke ONE previously-passing
PASS_TO_PASS test (`test_underscore_in_attribute_strip_signature_backslash`): it stopped escaping
a trailing `_` unconditionally, when it should only stop when `config.strip_signature_backslash`
is off. Strict gate fails on the 1 regression. The scaffold already re-ran PASS_TO_PASS and fed
the failing test name back, but as one undifferentiated entry in a flat "failing tests" list — no
signal that this was a *regression* needing a *conditional* fix.

**Generalization (no instance knowledge).**
1. `bench/swe_check.py` — `_verdict_breakdown()` reads the official `report.json` and splits the
   failing tests into **REGRESSIONS** (PASS_TO_PASS now failing = the patch broke a previously-
   passing behavior) vs **still-UNFIXED** FAIL_TO_PASS targets. `_mode_banner()` headlines the
   failure mode and, for regressions, instructs: *open each broken test's source, see what
   input/config/flag/branch it sets up, and make the fix CONDITIONAL so both the new behavior and
   the test's expectation hold — do not remove/invert behavior, gate it on the same condition.*
   This banner is prepended to the existing assertion/traceback detail.
2. `bench/swe_batch_setup.py` — `goal_text()` gains a general step: prefer **conditional** fixes,
   check whether a config flag / argument / branch still needs the old behavior, and treat a
   newly-failing previously-passing test as a regression (over-broad fix), not the bug.

Both use only swebench's own per-test categorization — they work for any repo/instance.

**Validation.** `bench/test_swe_check_feedback.py` — 6/6 hermetic unit tests (synthetic reports,
no live WSL, no burned-instance artifacts): regression-only, unfixed-only, both, resolved→empty
(no false alarm), missing-report→safe `([],[])`, truncation cap. Also spot-checked on real
reports: sphinx-7738 → exactly 1 regression (the strip_signature_backslash test); resolved
django-16595 → `(0,0)` (no false positive).

**Honest scope.** This is validated as a *mechanism* — the agent now receives a regression-vs-
unfixed split with mode-specific guidance. Whether it *lifts the pass rate* on this miss class
needs a controlled A/B on FRESH (non-holdout) instances; that is a separate eval campaign, not
claimed here.

---

## Class 2 — external-service test dependency (requests-2148, requests-2317)

**Symptom.** `test_requests.py` exercises a live `httpbin` (the public `httpbin.org`, which 503s /
is ~25 s/req from this host). At eval time the suite hit the public server and most failures were
`assert 503 == 200`. Neither requests patch is the cause; the model's fixes are largely correct
(requests-2317: 7/8 FAIL_TO_PASS + 130/133 PASS_TO_PASS pass).

**Generalization (no instance knowledge).** A hermetic local `httpbin` stand-in for any suite that
reads it:
- `bench/swe_httpbin.sh` + `bench/hb_server.py` — local httpbin on http:80 AND https:443
  (self-signed cert, SAN `DNS:httpbin.org, localhost, IP:172.17.0.1, 127.0.0.1`).
- `bench/swe_shim/sitecustomize.py` — when `SWE_HTTPBIN_URL` is set, injects into the container's
  `eval.sh`: `HTTPBIN_URL`, an `/etc/hosts` map `httpbin.org -> 172.17.0.1`, and (if a cert is
  given) decodes it to `/tmp/hbcert.pem` and exports `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`.
  No-op for repos that don't read httpbin.
- `bench/swe_check.py` — forwards `SWE_HTTPBIN_URL`/`SWE_HTTPBIN_CERT` into the WSL eval.

**Validation.** Cert-trust mechanism proven at the TLS level (general, not requests-specific):
`curl --cacert /opt/hb/cert.pem https://127.0.0.1/get` → 200; `--resolve httpbin.org:443:127.0.0.1
--cacert` → 200 (DNS-SAN match). Effect on the burned instance (not claimed, shown only as
capability evidence): requests-2148 went from ~33 failing tests to **3** with the local httpbin.

**Residual (honest).** The 3 remaining requests-2148 / requests-2317 failures are deep edge cases —
`pyopenssl_redirect` (needs pyOpenSSL + a real TLS redirect), `test_stream_timeout` (needs a
genuinely slow endpoint, e.g. httpbin `/drip`), `test_mixed_case_scheme_acceptable`. These are
environment-limited and the instances are burned, so they are left as known harness limitations,
not chased.

---

## Net

- 1 genuine model-miss class (config-branch regression) now has a domain-general scaffold response
  (regression-aware feedback + goal guidance), mechanism-validated (6/6 unit tests).
- 1 environment class (external httpbin) has a domain-general hermetic stand-in; common path works,
  deep TLS/timeout edge cases residual + burned.
- No instance-specific patches; holdout left sealed; burned instances excluded from score.

_2026-06-14._
