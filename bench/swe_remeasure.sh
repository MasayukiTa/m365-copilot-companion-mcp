#!/bin/sh
# Clean re-eval of the un-logged holdout instances (their worktrees still hold the model's patch)
# to CONFIRM the aggregate strict-resolved tally. Sequential = disk-safe. swe_check exit 0=resolved.
#
# NOTE: the inner `swe_check.py` spawns wsl.exe/docker which READ STDIN; a plain
# `while read ... done < file` lets that child consume the rest of the file and the loop
# exits after one iteration. Fix: read on FD 3, and give the child `</dev/null`.
# Resumable: an instance already present in the results file is skipped.
cd /c/Users/USER/companion-mcp
RES=.fleet/swe/_remeasure_results.txt
touch "$RES"
export SWE_HTTPBIN_URL="http://httpbin.org/"
export SWE_HTTPBIN_CERT="/opt/hb/cert.pem"
export SWE_EVAL_TIMEOUT_S=1500
export PYTHONIOENCODING=ascii:replace
n=0; res=0; total=$(grep -c . .fleet/swe/_remeasure.txt)
while IFS= read -r inst <&3; do
  [ -z "$inst" ] && continue
  n=$((n+1))
  if grep -q " $inst\$" "$RES" 2>/dev/null; then
    grep -q "^RESOLVED $inst\$" "$RES" && res=$((res+1))
    echo "[$n/$total] $inst -> (cached, skip)  (running resolved=$res)"
    continue
  fi
  if .venv/Scripts/python.exe bench/swe_check.py "$inst" </dev/null >/dev/null 2>&1; then
    res=$((res+1)); echo "RESOLVED $inst" >> "$RES"; st=RESOLVED
  else
    echo "NOT $inst" >> "$RES"; st=NOT
  fi
  echo "[$n/$total] $inst -> $st  (running resolved=$res)"
done 3< .fleet/swe/_remeasure.txt
echo "=== REMEASURE DONE: $res/$total of the un-logged re-confirmed resolved ==="
echo "=== n60 = 31 official-logged + $res confirmed = $((31+res))/60 ==="
echo "=== NOT-resolved this batch: ==="
grep '^NOT ' "$RES" || echo "(none)"
