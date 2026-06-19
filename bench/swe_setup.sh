#!/bin/sh
# SWE-bench eval host setup inside the WSL2 Alpine distro. Idempotent-ish.
set -e
echo "[1/4] build deps"
apk add --no-cache python3-dev gcc musl-dev git patch >/tmp/apk2.log 2>&1
echo "[2/4] venv"
[ -d /root/swe-venv ] || python3 -m venv /root/swe-venv
echo "[3/4] pip upgrade"
/root/swe-venv/bin/pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org --upgrade pip >/tmp/pip.log 2>&1
echo "[4/4] swebench + datasets (slow)"
/root/swe-venv/bin/pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org swebench datasets >>/tmp/pip.log 2>&1
/root/swe-venv/bin/python -c "import swebench, datasets, sys; sys.stdout.write('swebench import OK; datasets '+datasets.__version__+'\n')"
echo SETUP_DONE
