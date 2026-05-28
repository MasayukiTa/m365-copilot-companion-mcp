"""Process management.

Read-only listing is unauthenticated beyond the API key; sending signals
(kill) requires the IP unlock layer just like other mutating tools.
"""
from typing import Optional

from .security import require_unlocked


def process_list(
    name_contains: Optional[str] = None,
    limit: int = 100,
    sort_by: str = "cpu",
) -> str:
    """List running processes (PID, name, user, CPU%, RAM MB).

    Args:
        name_contains: Optional substring filter on process name (case-insensitive).
        limit: Maximum rows to return.
        sort_by: "cpu", "memory", "pid", or "name". Default "cpu".
    """
    try:
        import psutil
    except ImportError:
        return "[process_list error: psutil not installed]"
    try:
        # Prime cpu_percent so the second pass returns meaningful values.
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                proc.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # tiny pause not needed — second call to cpu_percent uses since-last delta

        rows = []
        needle = name_contains.lower() if name_contains else None
        for proc in psutil.process_iter(["pid", "name", "username"]):
            try:
                info = proc.info
                name = (info.get("name") or "").lower()
                if needle and needle not in name:
                    continue
                cpu = proc.cpu_percent(None)
                rss_mb = proc.memory_info().rss / (1024 * 1024)
                rows.append(
                    {
                        "pid": info["pid"],
                        "name": info.get("name") or "",
                        "user": (info.get("username") or "")[-20:],
                        "cpu": cpu,
                        "mem": rss_mb,
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        keyfn = {
            "cpu": lambda r: -r["cpu"],
            "memory": lambda r: -r["mem"],
            "pid": lambda r: r["pid"],
            "name": lambda r: r["name"].lower(),
        }.get(sort_by, lambda r: -r["cpu"])
        rows.sort(key=keyfn)
        rows = rows[:limit]

        lines = [f"{'PID':>6}  {'NAME':<28}  {'USER':<22}  {'CPU%':>5}  {'RAM_MB':>7}"]
        for r in rows:
            lines.append(
                f"{r['pid']:>6}  {r['name'][:28]:<28}  {r['user'][:22]:<22}  "
                f"{r['cpu']:>5.1f}  {r['mem']:>7.1f}"
            )
        lines.append(f"--- {len(rows)} process(es) shown")
        return "\n".join(lines)
    except Exception as e:
        return f"[process_list error: {type(e).__name__}: {e}]"


def process_info(pid: int) -> str:
    """Return detailed info about a single process: cmdline, cwd, open file count, threads."""
    try:
        import psutil
    except ImportError:
        return "[process_info error: psutil not installed]"
    try:
        p = psutil.Process(int(pid))
        info = p.as_dict(
            attrs=[
                "pid", "name", "username", "status", "create_time", "exe",
                "cmdline", "cwd", "num_threads",
            ],
            ad_value=None,
        )
        from datetime import datetime
        created = (
            datetime.fromtimestamp(info["create_time"]).isoformat(timespec="seconds")
            if info.get("create_time") else "(unknown)"
        )
        mem = p.memory_info()
        try:
            n_files = len(p.open_files())
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            n_files = -1
        lines = [
            f"pid: {info['pid']}",
            f"name: {info['name']}",
            f"user: {info.get('username') or ''}",
            f"status: {info.get('status') or ''}",
            f"started: {created}",
            f"exe: {info.get('exe') or ''}",
            f"cwd: {info.get('cwd') or ''}",
            f"threads: {info.get('num_threads') or '?'}",
            f"open_files: {n_files if n_files >= 0 else '(no permission)'}",
            f"rss: {mem.rss / (1024 * 1024):.1f} MB",
            f"vms: {mem.vms / (1024 * 1024):.1f} MB",
            "cmdline:",
            "  " + " ".join(info.get("cmdline") or ["(none)"]),
        ]
        return "\n".join(lines)
    except psutil.NoSuchProcess:
        return f"[process_info: no process with pid={pid}]"
    except Exception as e:
        return f"[process_info error: {type(e).__name__}: {e}]"


def process_kill(pid: int, force: bool = False) -> str:
    """Terminate a running process by PID.

    Args:
        pid: Target PID.
        force: If True use SIGKILL / TerminateProcess immediately. If False
            send a graceful terminate first and wait briefly.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        import psutil
    except ImportError:
        return "[process_kill error: psutil not installed]"
    try:
        p = psutil.Process(int(pid))
        name = p.name()
        if force:
            p.kill()
            return f"killed (forced) pid={pid} name={name}"
        p.terminate()
        try:
            p.wait(timeout=5)
            return f"terminated pid={pid} name={name}"
        except psutil.TimeoutExpired:
            p.kill()
            return f"force-killed pid={pid} name={name} (graceful terminate timed out)"
    except psutil.NoSuchProcess:
        return f"[process_kill: no process with pid={pid}]"
    except psutil.AccessDenied:
        return f"[process_kill error: access denied for pid={pid} (need admin?)]"
    except Exception as e:
        return f"[process_kill error: {type(e).__name__}: {e}]"
