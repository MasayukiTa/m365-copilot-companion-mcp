"""
GAIA tool-augmented evaluation pipeline — full end-to-end orchestrator.
Designed to run DETACHED (Windows Scheduled Task) and survive parent exit.

Steps:
  1. Kill any existing relay.openai_endpoint_server processes
  2. Start :8011, wait for it to be up, settle
  3. Run full eval (runner.py)
  4. Build keep_results.json + retry_ids.json
  5. If retries needed: run retry_controller.py
  6. Compute and log final scores
  7. Leave :8011 running
"""

import ast  # noqa — stdlib only, validate at top
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
FLEET_GAIA = REPO_ROOT / ".fleet" / "gaia"
FLEET_BENCH = REPO_ROOT / ".fleet" / "bench"
PIPELINE_LOG = FLEET_GAIA / "pipeline.log"
ENDPOINT_LOG = FLEET_GAIA / "pipeline_endpoint.log"
KEEP_RESULTS_JSON = FLEET_GAIA / "keep_results.json"
RETRY_IDS_JSON = FLEET_GAIA / "retry_ids.json"
FINAL_JSON = FLEET_GAIA / "gaia_final_127.json"
PIPELINE_FINAL_JSON = FLEET_GAIA / "pipeline_final.json"

RELAY_URL = "http://127.0.0.1:8011/v1/models"
RELAY_WAIT_SECS = 90
RELAY_SETTLE_SECS = 45

# Strings whose presence in a prediction mark it as infra-failed
INFRA_FAIL_SUBSTRINGS = [
    "申し訳",
    "それに応答",
    "HTTP 502",
    "composer still holds",
    "SystemError",
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _ensure_dirs():
    FLEET_GAIA.mkdir(parents=True, exist_ok=True)
    FLEET_BENCH.mkdir(parents=True, exist_ok=True)


_log_fh = None


def _open_log():
    global _log_fh
    _ensure_dirs()
    _log_fh = open(PIPELINE_LOG, "a", encoding="utf-8", buffering=1)


def log(msg: str):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


# ---------------------------------------------------------------------------
# Step 1: Kill existing relay processes
# ---------------------------------------------------------------------------
def kill_relay():
    log("STEP 1: Killing any existing relay.openai_endpoint_server processes …")
    # Try psutil first; fall back to PowerShell Get-CimInstance
    killed = False
    try:
        import psutil  # type: ignore
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "openai_endpoint_server" in cmdline:
                    log(f"  psutil: killing pid {proc.pid}")
                    proc.kill()
                    killed = True
            except Exception:
                pass
        if killed:
            time.sleep(2)
        else:
            log("  psutil: no matching processes found")
    except ImportError:
        log("  psutil not available; falling back to PowerShell")
        _kill_relay_powershell()


def _kill_relay_powershell():
    """Use PowerShell Get-CimInstance / taskkill to kill relay processes."""
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter "
            "\"CommandLine LIKE '%openai_endpoint_server%'\" | "
            "ForEach-Object { taskkill /PID $_.ProcessId /F }"
        )
        result = subprocess.run(
            ["powershell", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=20,
        )
        out = (result.stdout + result.stderr).strip()
        if out:
            log(f"  powershell kill output: {out[:400]}")
        time.sleep(2)
    except Exception as exc:
        log(f"  WARNING: relay kill via powershell failed (ignored): {exc}")


# ---------------------------------------------------------------------------
# Step 2: Start :8011 relay
# ---------------------------------------------------------------------------
def build_relay_env(relay_reset_every="6"):
    env = os.environ.copy()
    env["RELAY_RESET_EVERY"] = relay_reset_every
    env["MCP_GAIA_TOOLAUG"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def start_relay(relay_reset_every="6"):
    log("STEP 2: Starting relay endpoint :8011 …")
    env = build_relay_env(relay_reset_every)
    endpoint_log_fh = open(ENDPOINT_LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV_PY), "-m", "relay.openai_endpoint_server"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=endpoint_log_fh,
        stderr=endpoint_log_fh,
        # detach from job object so it survives parent exit (Windows)
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )
    log(f"  Relay PID: {proc.pid}")

    # Poll until up
    log(f"  Polling {RELAY_URL} for up to {RELAY_WAIT_SECS}s …")
    deadline = time.time() + RELAY_WAIT_SECS
    up = False
    while time.time() < deadline:
        try:
            resp = urlopen(RELAY_URL, timeout=5)
            log(f"  Relay responded HTTP {resp.status} — up!")
            up = True
            break
        except URLError as exc:
            reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
            # HTTP 401 means server is alive (auth required) — treat as up
            if "401" in reason or "Unauthorized" in reason:
                log("  Relay returned 401 (auth required) — treating as up.")
                up = True
                break
            time.sleep(3)
        except Exception as exc:
            # HTTPError (non-urllib) — server is responding
            err_str = str(exc)
            if "401" in err_str or "403" in err_str:
                log(f"  Relay HTTP error {err_str} — treating as up.")
                up = True
                break
            time.sleep(3)

    if not up:
        try:
            proc.terminate()
        except Exception:
            pass
        raise RuntimeError("relay did not respond within %ds" % RELAY_WAIT_SECS)

    log(f"  Sleeping {RELAY_SETTLE_SECS}s for MCP connector to settle …")
    time.sleep(RELAY_SETTLE_SECS)
    return proc


# ---------------------------------------------------------------------------
# Step 3: Full eval
# ---------------------------------------------------------------------------
def run_full_eval():
    log("STEP 3: Running full GAIA eval (runner.py, all 127 questions) …")
    env = build_relay_env("6")
    result = subprocess.run(
        [str(VENV_PY), "-u", "bench/gaia/runner.py"],
        cwd=str(REPO_ROOT),
        env=env,
        timeout=None,  # may run for hours
    )
    log(f"  runner.py exit code: {result.returncode}")
    return result.returncode


# ---------------------------------------------------------------------------
# Step 4: Build keep / retry splits
# ---------------------------------------------------------------------------
def _is_infra_fail(prediction) -> bool:
    """Return True if prediction looks like an infra failure, not a real answer."""
    if not isinstance(prediction, str):
        return True
    if not prediction.strip():
        return True
    for sub in INFRA_FAIL_SUBSTRINGS:
        if sub in prediction:
            return True
    return False


def find_newest_full_results(not_before: float | None = None) -> Path | None:
    pattern = str(FLEET_BENCH / "gaia_full_*.json")
    matches = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not_before is not None:
        matches = [path for path in matches if os.path.getmtime(path) >= not_before]
    if not matches:
        return None
    return Path(matches[-1])


def build_keep_retry_split(not_before: float | None = None):
    log("STEP 4: Building keep_results.json and retry_ids.json …")
    newest = find_newest_full_results(not_before=not_before)
    if newest is None:
        log("  ERROR: No gaia_full_*.json found in .fleet/bench/. Cannot build split.")
        raise RuntimeError("no current-run gaia_full_*.json result was produced")

    log(f"  Loading results from: {newest}")
    with open(newest, encoding="utf-8") as fh:
        rows = json.load(fh)

    if not isinstance(rows, list):
        # Might be wrapped in a dict
        if isinstance(rows, dict):
            rows = rows.get("results", rows.get("rows", list(rows.values())))
        else:
            rows = []

    task_ids = [row.get("task_id", row.get("id", "")) for row in rows
                if isinstance(row, dict)]
    if len(rows) != 127 or len(set(task_ids)) != 127 or any(not task_id for task_id in task_ids):
        raise RuntimeError(
            "full evaluation result is incomplete: rows=%d unique_nonempty_ids=%d expected=127" %
            (len(rows), len({task_id for task_id in task_ids if task_id})))

    keep_rows = []
    retry_ids = []
    for row in rows:
        pred = row.get("prediction", row.get("answer", ""))
        if _is_infra_fail(pred):
            retry_ids.append(row.get("task_id", row.get("id", "")))
        else:
            keep_rows.append(row)

    log(f"  Total rows: {len(rows)}  keep: {len(keep_rows)}  retry: {len(retry_ids)}")

    with open(KEEP_RESULTS_JSON, "w", encoding="utf-8") as fh:
        json.dump(keep_rows, fh, ensure_ascii=False, indent=2)
    with open(RETRY_IDS_JSON, "w", encoding="utf-8") as fh:
        json.dump(retry_ids, fh, ensure_ascii=False, indent=2)

    log(f"  Written: {KEEP_RESULTS_JSON}")
    log(f"  Written: {RETRY_IDS_JSON}")
    return keep_rows, retry_ids


# ---------------------------------------------------------------------------
# Step 5: Retry controller
# ---------------------------------------------------------------------------
def run_retry_controller():
    log("STEP 5: Running retry_controller.py …")
    # Kill relay first — controller restarts it per chunk itself
    kill_relay()
    time.sleep(2)

    env = build_relay_env("999")
    env["RELAY_RESET_EVERY"] = "999"  # controller manages restart
    result = subprocess.run(
        [str(VENV_PY), "-u", "bench/gaia/retry_controller.py"],
        cwd=str(REPO_ROOT),
        env=env,
        timeout=None,
    )
    log(f"  retry_controller.py exit code: {result.returncode}")
    return result.returncode


# ---------------------------------------------------------------------------
# Step 6: Compute final scores
# ---------------------------------------------------------------------------
def _level_key(row) -> str:
    lv = row.get("level", row.get("Level", "?"))
    return str(lv)


def compute_final(keep_rows, retry_ids, not_before: float | None = None):
    log("STEP 6: Computing final scores …")

    all_rows: list[dict] = []

    # Prefer the controller-merged output
    final_is_current = (FINAL_JSON.exists() and
                        (not_before is None or FINAL_JSON.stat().st_mtime >= not_before))
    if final_is_current:
        log(f"  Loading controller output: {FINAL_JSON}")
        with open(FINAL_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            all_rows = data
        elif isinstance(data, dict):
            all_rows = data.get("results", data.get("rows", []))
        else:
            all_rows = []
    else:
        log("  gaia_final_127.json not found; merging keep + chunk results manually.")
        all_rows = list(keep_rows)
        chunk_pattern = str(FLEET_GAIA / "chunk_*_res.json")
        chunk_files = sorted(glob.glob(chunk_pattern))
        if not_before is not None:
            chunk_files = [path for path in chunk_files if os.path.getmtime(path) >= not_before]
        for chunk_file in chunk_files:
            try:
                with open(chunk_file, encoding="utf-8") as fh:
                    chunk_data = json.load(fh)
                chunk_rows = chunk_data if isinstance(chunk_data, list) else []
                for row in chunk_rows:
                    if not row.get("error"):
                        all_rows.append(row)
            except Exception as exc:
                log(f"  WARNING: could not load {chunk_file}: {exc}")

    task_ids = [r.get("task_id", r.get("id", "")) for r in all_rows
                if isinstance(r, dict)]
    unique_ids = {task_id for task_id in task_ids if task_id}
    total = len(all_rows)
    correct = sum(1 for r in all_rows if r.get("correct") is True or r.get("correct") == 1)

    # Per-level breakdown
    per_level: dict[str, dict] = {}
    for row in all_rows:
        lv = _level_key(row)
        if lv not in per_level:
            per_level[lv] = {"answered": 0, "correct": 0}
        per_level[lv]["answered"] += 1
        if row.get("correct") is True or row.get("correct") == 1:
            per_level[lv]["correct"] += 1

    answered = total
    pct = (correct / 127 * 100) if 127 > 0 else 0.0
    unrecovered = [tid for tid in retry_ids if not any(r.get("task_id") == tid for r in all_rows)]

    complete = (total == 127 and len(unique_ids) == 127 and not unrecovered)
    summary_line = (
        f"PIPELINE {'FINAL' if complete else 'PARTIAL'}: {correct}/127 = {pct:.1f}%  "
        f"(answered {answered}, unrecovered {len(unrecovered)})"
    )
    level_lines = []
    for lv in sorted(per_level.keys()):
        lv_data = per_level[lv]
        lv_pct = lv_data["correct"] / lv_data["answered"] * 100 if lv_data["answered"] else 0.0
        level_lines.append(f"  L{lv}: {lv_data['correct']}/{lv_data['answered']} = {lv_pct:.1f}%")

    log(summary_line)
    for line in level_lines:
        log(line)

    # Write pipeline_final.json
    final_payload = {
        "status": "complete" if complete else "partial",
        "answered": answered,
        "correct": correct,
        "score_of_127": round(pct, 2),
        "per_level": per_level,
        "unrecovered": unrecovered,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(PIPELINE_FINAL_JSON, "w", encoding="utf-8") as fh:
        json.dump(final_payload, fh, ensure_ascii=False, indent=2)
    log(f"  Written: {PIPELINE_FINAL_JSON}")

    return final_payload


def _write_failed_pipeline(errors):
    payload = {
        "status": "failed",
        "errors": [str(error) for error in errors],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(PIPELINE_FINAL_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    run_started_at = time.time()
    _open_log()
    log("=" * 70)
    log("GAIA PIPELINE START")
    log(f"  repo root : {REPO_ROOT}")
    log(f"  python    : {VENV_PY}")
    log("=" * 70)

    keep_rows: list = []
    retry_ids: list = []
    errors: list[str] = []

    # ---- Step 1: Kill relay ------------------------------------------------
    try:
        kill_relay()
    except Exception as exc:
        log(f"Step 1 ERROR (ignored): {exc}")

    # ---- Step 2: Start relay -----------------------------------------------
    relay_proc = None
    try:
        relay_proc = start_relay(relay_reset_every="6")
    except Exception as exc:
        log(f"Step 2 ERROR starting relay: {exc}")
        errors.append("step 2: %s" % exc)

    # ---- Step 3: Full eval -------------------------------------------------
    if not errors:
        try:
            eval_rc = run_full_eval()
            if eval_rc != 0:
                raise RuntimeError("runner.py exited with code %d" % eval_rc)
        except Exception as exc:
            log(f"Step 3 ERROR running full eval: {exc}")
            errors.append("step 3: %s" % exc)

    # ---- Step 4: Split keep / retry ----------------------------------------
    if not errors:
        try:
            keep_rows, retry_ids = build_keep_retry_split(not_before=run_started_at)
        except Exception as exc:
            log(f"Step 4 ERROR building split: {exc}")
            errors.append("step 4: %s" % exc)

    # ---- Step 5: Retry controller ------------------------------------------
    if retry_ids and not errors:
        try:
            retry_rc = run_retry_controller()
            if retry_rc != 0:
                raise RuntimeError("retry_controller.py exited with code %d" % retry_rc)
        except Exception as exc:
            log(f"Step 5 ERROR in retry controller: {exc}")
            errors.append("step 5: %s" % exc)
    elif not errors:
        log("STEP 5: No retry_ids — skipping retry controller.")

    # ---- Step 6: Final scores ----------------------------------------------
    if not errors:
        try:
            final_payload = compute_final(keep_rows, retry_ids, not_before=run_started_at)
            if final_payload.get("status") != "complete":
                raise RuntimeError(
                    "current run did not produce 127 unique complete results "
                    "(answered=%s unrecovered=%s)" % (
                        final_payload.get("answered"),
                        len(final_payload.get("unrecovered") or [])))
        except Exception as exc:
            log(f"Step 6 ERROR computing final: {exc}")
            errors.append("step 6: %s" % exc)

    if errors:
        _write_failed_pipeline(errors)

    # ---- Step 7: Leave :8011 running ---------------------------------------
    log("STEP 7: Leaving relay :8011 running (PID kept alive).")
    if relay_proc is not None:
        poll = relay_proc.poll()
        if poll is None:
            log(f"  Relay PID {relay_proc.pid} still running — done.")
        else:
            log(f"  Relay PID {relay_proc.pid} already exited with code {poll}.")

    log("=" * 70)
    log("GAIA PIPELINE FAILED" if errors else "GAIA PIPELINE COMPLETE")
    log("=" * 70)

    if _log_fh:
        _log_fh.close()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
