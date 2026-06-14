#!/bin/sh
# Dump the concrete failure evidence for the 3 not_resolved holdout instances.
LD=/root/swe/logs/run_evaluation
for inst in psf__requests-2148 psf__requests-2317 sphinx-doc__sphinx-7738; do
  rid=$(printf '%s' "$inst" | sed 's/__/_/g')
  d="$LD/agent_$rid/companion/$inst"
  echo "############################################################"
  echo "## $inst"
  echo "############################################################"
  rep="$d/report.json"
  if [ -f "$rep" ]; then
    echo "--- report.json (resolved + which tests fall where) ---"
    grep -E '"resolved"|"FAIL_TO_PASS"|"PASS_TO_PASS"|"f2p_success"|"f2p_failure"|"p2p_success"|"p2p_failure"' "$rep" 2>/dev/null
    echo "--- failing test ids (success/failure breakdown) ---"
    python3 - "$rep" 2>/dev/null <<'PY'
import json,sys
r=json.load(open(sys.argv[1]))
# report is {inst: {...}}
for k,v in r.items():
    t=v.get("tests_status",{})
    for cat in ("FAIL_TO_PASS","PASS_TO_PASS"):
        c=t.get(cat,{})
        fails=c.get("failure",[]) if isinstance(c,dict) else []
        succ=c.get("success",[]) if isinstance(c,dict) else []
        print(f"  {cat}: {len(succ)} pass / {len(fails)} FAIL")
        for f in fails[:8]:
            print(f"     FAIL-> {f}")
PY
  else
    echo "(no report.json)"
  fi
  to="$d/test_output.txt"
  if [ -f "$to" ]; then
    echo "--- test_output.txt: error/exception signatures ---"
    grep -nE 'Error|Exception|assert|FAILED|SSL|Connection|Timeout|httpbin|Max retries|refused|certificate' "$to" 2>/dev/null | head -20
  else
    echo "(no test_output.txt)"
  fi
  echo ""
done
