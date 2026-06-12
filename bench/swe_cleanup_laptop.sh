#!/bin/sh
# Stop the SWE-bench eval and free the heavy images on the laptop WSL2 (eval moves to the eval host).
pkill -f run_evaluation 2>/dev/null
sleep 2
CIDS=$(docker ps -aq 2>/dev/null)
[ -n "$CIDS" ] && docker rm -f $CIDS 2>/dev/null
IIDS=$(docker images -q "swebench/*" 2>/dev/null)
[ -n "$IIDS" ] && docker rmi -f $IIDS 2>/dev/null
echo "--- images after cleanup ---"
docker images 2>/dev/null
echo "--- disk ---"
df -h / | tail -1
