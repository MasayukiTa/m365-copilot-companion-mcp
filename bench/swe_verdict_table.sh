#!/bin/sh
# Build the authoritative holdout-60 verdict table from official swebench report.json artifacts.
HL=/mnt/c/Users/USER/companion-mcp/.fleet/swe/holdout_dev.txt
LD=/root/swe/logs/run_evaluation
res=0; nores=0; noreport=0
OUT=/mnt/c/Users/USER/companion-mcp/.fleet/swe/_verdict_table.txt
: > "$OUT"
while IFS= read -r raw; do
  inst=$(printf '%s' "$raw" | tr -d '\r' | tr -d ' ')
  [ -z "$inst" ] && continue
  rid=$(printf '%s' "$inst" | sed 's/__/_/g')
  rep="$LD/agent_$rid/companion/$inst/report.json"
  if [ -f "$rep" ]; then
    if grep -q '"resolved": true' "$rep"; then
      v=RESOLVED; res=$((res+1))
    else
      v=not_resolved; nores=$((nores+1))
    fi
  else
    v=NO_REPORT; noreport=$((noreport+1))
  fi
  printf '%-42s %s\n' "$inst" "$v" >> "$OUT"
done < "$HL"
cat "$OUT"
echo "================================================"
echo "RESOLVED=$res  not_resolved=$nores  NO_REPORT=$noreport  (of 60)"
