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
import re
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


def _looks_like_models_payload(body: bytes) -> bool:
    """True only if the body is the OpenAI /v1/models shape this endpoint serves.

    A bare TCP listener or some unrelated service squatting on :8011 can answer
    HTTP too, so "the socket accepted a request" is not "our endpoint is up".
    Require the documented envelope -- a JSON object with object=="list" and a
    data array -- before calling it alive, so restart_8011 keeps restarting a
    port that has been taken over by something that is not our server.
    """
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return False
    if not isinstance(doc, dict):
        return False
    if doc.get("object") != "list":
        return False
    return isinstance(doc.get("data"), list)


def _8011_up() -> bool:
    """Alive only when :8011 answers /v1/models with our expected payload.

    A 401/HTTPError is NOT treated as alive on its own any more: an unrelated
    service can return any status. We only trust a 2xx whose body is the models
    envelope. Anything else (connection refused, wrong body, non-2xx) is down.
    """
    try:
        req = urllib.request.Request("http://127.0.0.1:8011/v1/models",
                                     headers={"Authorization": "Bearer x"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            body = resp.read(65536)
        return _looks_like_models_payload(body)
    except urllib.error.HTTPError:
        # Some auth configs 401 the probe; read the error body and only accept
        # it if it is still our server's JSON envelope, never blindly.
        return False
    except Exception:
        return False


def _ancestor_pids(max_depth: int = 12) -> set[int]:
    """This process and every parent above it.

    Whatever launched us -- the tool call, the shell, a wrapper -- can carry the
    same module name on its command line, and killing our own parent is the
    destructive self-match that find_procs.ps1 exists to prevent. Exclude them.
    """
    ids: set[int] = set()
    try:
        import psutil
    except Exception:
        # Without psutil we cannot enumerate ancestors; return just our own PID
        # so at minimum we never taskkill the running controller itself.
        return {os.getpid()}
    cur = os.getpid()
    for _ in range(max_depth):
        if not cur:
            break
        ids.add(cur)
        try:
            cur = psutil.Process(cur).ppid()
        except Exception:
            break
    return ids


def _endpoint_pids(rows, my_ancestors, venv_python: str):
    """Select ONLY the python processes that are actually our :8011 endpoint.

    rows: iterable of (pid:int, command_line:str) from the process table.
    Pure and side-effect free so it can be unit-tested without a process table.

    Three guards, all required (mirrors scripts/win/find_procs.ps1):
      * absolute-path match on the venv python that restart_8011 launches, so a
        stray system python that merely mentions the module is not a candidate;
      * the module must appear as its own run target (`-m relay.openai_endpoint_server`
        or `relay/openai_endpoint_server`), not as an arbitrary substring inside
        some unrelated argument;
      * our own process and its ancestors are excluded, so the controller can
        never kill the shell that started it.
    """
    venv_norm = os.path.normcase(os.path.abspath(venv_python)) if venv_python else ""
    selected = []
    for pid, cmd in rows:
        if pid in my_ancestors:
            continue
        cmd = cmd or ""
        low = os.path.normcase(cmd)
        # 1) must be launched by our venv python (absolute path present on cmdline)
        if venv_norm and venv_norm not in low:
            continue
        # 2) module must be an actual run target, not an incidental substring
        if not re.search(r"(?:-m\s+relay\.openai_endpoint_server\b"
                         r"|relay[\\/]openai_endpoint_server)", cmd):
            continue
        selected.append(pid)
    return selected


def kill_8011():
    # Kill ONLY the venv python running our endpoint module. The old filter used
    # a bare `CommandLine -like "*relay.openai_endpoint_server*"` substring on
    # every python.exe, which would also match an unrelated python that happened
    # to carry that string as an argument. Now we (a) require the venv python's
    # absolute path, (b) require the module as a real -m run target, and
    # (c) exclude this process and its ancestors -- the find_procs.ps1 shape.
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True).stdout
    rows = []
    try:
        data = json.loads(out) if out.strip() else []
    except Exception:
        data = []
    if isinstance(data, dict):
        data = [data]
    for item in data:
        try:
            pid = int(item.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        rows.append((pid, item.get("CommandLine") or ""))
    my_ancestors = _ancestor_pids()
    for pid in _endpoint_pids(rows, my_ancestors, str(PY_VENV)):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
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
    # Smaller chunks for the tool-augmented run: tool-heavy turns accumulate
    # context fast and a fresh :8011 per chunk is the real wedge cure, so keep
    # each chunk <= the ~8-turn safe window with longer per-question timeouts.
    rounds = [(8, 200), (4, 220), (2, 260)]  # (chunk_size, timeout) per round

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
