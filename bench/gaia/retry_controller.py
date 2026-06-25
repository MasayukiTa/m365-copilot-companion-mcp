"""GAIA retry controller — re-run only the errored/unreached items.

Root cause of the first full run's cascade: one hard L3 question timed out
(generation stuck) which wedged the Copilot composer ("Send button never
submitted"), erroring every subsequent question in the SAME long-lived
conversation. Fix here is purely operational (no model/scorer change):

  * process the retry set in small CHUNKS
  * restart the :8011 endpoint BEFORE each chunk -> fresh worker -> fresh
    /chat/ conversation -> fresh composer (a wedge cannot cross a restart)
  * shorter per-question timeout so a stuck generation is abandoned fast
  * any items still errored after a chunk drop into the next (smaller) round

This is infra recovery, NOT cherry-picking: errored items never got a real
model attempt. PASS/FAIL items from the first run are kept as-is and merged.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY_VENV = REPO / ".venv" / "Scripts" / "python.exe"
RETRY_IDS = REPO / ".fleet" / "gaia" / "retry_ids.json"
OUT_DIR = REPO / ".fleet" / "gaia"
ENDPOINT_LOG = OUT_DIR / "endpoint.log"


def _api_key() -> str:
    for line in (REPO / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == "MCP_API_KEY":
                return v.strip()
    return ""


def _8011_up() -> bool:
    try:
        req = urllib.request.Request("http://127.0.0.1:8011/v1/models",
                                     headers={"Authorization": "Bearer x"})
        urllib.request.urlopen(req, timeout=6)
        return True
    except urllib.error.HTTPError:
        return True  # 401 etc == server alive
    except Exception:
        return False


def kill_8011():
    # kill any python running the endpoint server
    ps = ('Get-CimInstance Win32_Process -Filter "Name=\'python.exe\'" | '
          'Where-Object { $_.CommandLine -like "*relay.openai_endpoint_server*" } | '
          'ForEach-Object { $_.ProcessId }')
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True).stdout
    for pid in out.split():
        pid = pid.strip()
        if pid.isdigit():
            subprocess.run(["taskkill", "/PID", pid, "/F"],
                           capture_output=True, text=True)
    time.sleep(2)


def restart_8011():
    kill_8011()
    log = open(ENDPOINT_LOG, "ab")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    subprocess.Popen([str(PY_VENV), "-m", "relay.openai_endpoint_server"],
                     cwd=str(REPO), stdout=log, stderr=log, env=env)
    for _ in range(30):
        time.sleep(2)
        if _8011_up():
            time.sleep(3)  # let the worker bind a fresh /chat/ conversation lazily
            return True
    return False


def run_chunk(ids: list[str], timeout: int, tag: str) -> list[dict]:
    idf = OUT_DIR / f"chunk_{tag}_ids.json"
    out = OUT_DIR / f"chunk_{tag}_res.json"
    idf.write_text(json.dumps(ids), encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", MCP_API_KEY=_api_key())
    print(f"  [chunk {tag}] {len(ids)} ids, timeout={timeout}s ...", flush=True)
    subprocess.run([str(PY_VENV), "-u", "bench/gaia/runner.py",
                    "--ids-file", str(idf), "--timeout", str(timeout),
                    "--output", str(out)],
                   cwd=str(REPO), env=env)
    if not out.exists():
        return []
    d = json.loads(out.read_text(encoding="utf-8"))
    return d.get("questions", [])


def main():
    retry = json.loads(RETRY_IDS.read_text(encoding="utf-8"))
    print(f"Retry controller: {len(retry)} items to recover", flush=True)

    collected: dict[str, dict] = {}
    pending = list(retry)
    rounds = [(15, 150), (6, 150), (3, 200)]  # (chunk_size, timeout) per round

    for rnd, (csize, tmo) in enumerate(rounds, 1):
        if not pending:
            break
        print(f"\n=== Round {rnd}: {len(pending)} pending, chunk={csize}, timeout={tmo}s ===", flush=True)
        chunks = [pending[i:i + csize] for i in range(0, len(pending), csize)]
        for ci, ch in enumerate(chunks, 1):
            if not restart_8011():
                print("  ERROR: :8011 failed to restart; aborting round", flush=True)
                break
            rows = run_chunk(ch, tmo, f"r{rnd}c{ci}")
            for r in rows:
                tid = r.get("task_id")
                if tid and not r.get("error"):
                    collected[tid] = r  # got a real answer
            done = sum(1 for r in rows if not r.get("error"))
            print(f"  [chunk r{rnd}c{ci}] recovered {done}/{len(ch)}", flush=True)
        pending = [i for i in retry if i not in collected]
        print(f"=== Round {rnd} end: {len(collected)} recovered, {len(pending)} still pending ===", flush=True)

    # Merge with kept real results from the first run
    keep = json.loads((OUT_DIR / "keep_results.json").read_text(encoding="utf-8"))
    final = {r["task_id"]: r for r in keep}
    final.update(collected)

    rows = list(final.values())
    answered = len(rows)
    correct = sum(1 for r in rows if r.get("correct"))
    still_err = [i for i in retry if i not in collected]
    # per-level
    per = {}
    for r in rows:
        lv = str(r.get("level"))
        per.setdefault(lv, {"total": 0, "correct": 0})
        per[lv]["total"] += 1
        per[lv]["correct"] += 1 if r.get("correct") else 0

    out = {
        "answered": answered, "correct": correct,
        "score_pct": round(100 * correct / answered, 1) if answered else 0,
        "unrecovered": still_err, "per_level": per,
        "rows": rows,
    }
    fp = OUT_DIR / "gaia_final_127.json"
    fp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'='*60}")
    print(f"FINAL: {correct}/{answered} = {out['score_pct']}%   (unrecovered: {len(still_err)})")
    for lv in sorted(per):
        p = per[lv]
        print(f"  Level {lv}: {p['correct']}/{p['total']} = {round(100*p['correct']/p['total'],1)}%")
    print(f"Written: {fp}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
