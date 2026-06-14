# Post-reboot runbook — regression-feedback A/B + larger-N confirmation

State at handoff (2026-06-14): holdout n=60 = **57/60 (95.0%)** confirmed from official artifacts.
Generalization fixes landed (regression-aware feedback + httpbin hermeticity), unit-tested 6/6.
Reboot chosen to reset the pagefile baseline + apply `.wslconfig memory=6GB` before the next
multi-hour eval campaign. Nothing was in flight; all state is on disk.

## 0. Verify the clean baseline (right after reboot)
```
# WSL VM is now capped (memory=6GB). Confirm it took effect:
wsl -d MiasmaLab -- free -m            # total should be ~6000, not ~16000
# C: headroom (want > 10GB before a long run):
#   PowerShell: Get-PSDrive C
# Start dockerd inside WSL (eval needs it):
wsl -d MiasmaLab -- sh -c 'pgrep dockerd >/dev/null || (nohup dockerd >/tmp/dockerd.log 2>&1 & sleep 8); docker info >/dev/null 2>&1 && echo dockerd OK'
```
Optional (only if you want concurrency > 1): free DtsApo4Service (~1.9GB, needs UAC; revives on
next reboot). Heavy cold builds are disk-bound — keep concurrency low (sequential-ish) per
[[swe-holdout-hardware-wall]].

## 1. Relaunch the local httpbin (only needed if the slice includes requests instances)
```
bash bench/swe_httpbin.sh      # http:80 + https:443, self-signed SAN=httpbin.org
```
Cert-trust is already proven (`curl --cacert` → 200). For requests instances, export before eval:
`SWE_HTTPBIN_URL=http://httpbin.org/ SWE_HTTPBIN_CERT=/opt/hb/cert.pem`.

## 2. The A/B experiment — does the regression-aware feedback lift recovery?

**Toggle:** `SWE_NO_REGRESSION_FEEDBACK=1` = CONTROL (old flat feedback). Unset = TREATMENT
(regression-vs-unfixed split + gating guidance). Implemented in `bench/swe_check.py`.

**Fresh slice (zero holdout contamination):** pick from `train_pool.txt` MINUS `holdout_dev.txt`
MINUS the burned set {requests-2148, requests-2317, sphinx-7738, matplotlib-18869}. 180 eligible.
Suggested balanced slice (~24, regression-rich repos favored — sympy/django/sklearn/sphinx):
```
.venv\Scripts\python.exe - <<'PY'
import os, json
base=r".fleet\swe"
load=lambda f:[l.strip() for l in open(os.path.join(base,f),encoding="utf-8") if l.strip()]
train=load("train_pool.txt"); holdout=set(load("holdout_dev.txt"))
burned={"psf__requests-2148","psf__requests-2317","sphinx-doc__sphinx-7738","matplotlib__matplotlib-18869"}
fresh=[t for t in train if t not in holdout and t not in burned]
# deterministic balanced pick: first K per chosen repo (NO randomness -- reproducible)
from collections import defaultdict
by=defaultdict(list)
for t in fresh: by[t.split("__")[0]].append(t)
slice_=[]
for repo,k in [("sympy",8),("django",6),("scikit-learn",4),("sphinx-doc",3),("pytest-dev",3)]:
    slice_+=sorted(by[repo])[:k]
open(os.path.join(base,"_ab_slice.txt"),"w").write("\n".join(slice_)+"\n")
print("A/B slice =",len(slice_)); print("\n".join(slice_))
PY
```

**Protocol (paired — same instances, both arms):**
1. Build the instance spec for the slice (the holdout spec-builder is repo-agnostic; reuse it or
   `swe_repos_setup_batch.py` to stage worktrees for the slice ids).
2. TREATMENT run: `swe_run_until_done.py` over the slice (regression feedback ON by default).
   Record resolved set + the per-instance runlog (`.fleet/runlogs/` verify events).
3. Reset the same worktrees, CONTROL run: same slice with
   `SWE_NO_REGRESSION_FEEDBACK=1` in the fleet env. Record resolved set + runlogs.
4. **Primary metric (targeted, not just aggregate):** of instances whose verify log shows a
   PASS_TO_PASS regression at any attempt, what fraction ended RESOLVED — TREATMENT vs CONTROL.
   The aggregate resolve-rate delta is the secondary metric (diluted by instances that never
   regress, so it will move less — report both, lead with the regression-subpopulation rate).
5. Honest reporting: small N → report counts + a binomial CI, not a bare percentage. The slice is
   fresh and sealed-separate from the holdout, so a positive result is a clean, claimable lift.

**Needs:** the live agent driver (m365 Copilot) → the MCP unlock must be authorized by the user
first (the credential cannot be auto-used). Confirm unlock, then launch.

## 3. Larger-N holdout confirmation (optional, after the A/B)
To tighten the 95% beyond n=60, draw a SECOND sealed holdout from the remaining SWE-bench Lite
pool (NOT train_pool, NOT the current holdout) and run it once under the (now generalized)
scaffold. Same strict gate, same official-report-json source of truth (`swe_verdict_table.sh`
pattern). Keep it sealed: do not debug on it. This grows the trustworthy sample without
re-touching burned/holdout instances.

## Artifacts
- Scorecard: `bench/SCORECARD_holdout60.md`  (57/60)
- Generalization + validation: `bench/VALIDATION_generalization_fixes.md`
- Verdict table builder: `bench/swe_verdict_table.sh` → `.fleet/swe/_verdict_table.txt`
- Feedback unit tests: `bench/test_swe_check_feedback.py` (6/6)
