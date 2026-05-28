"""Windows registry read-only access.

Useful for answering "what version of X is installed?", "is this driver
present?", "what's the default browser?" and similar diagnostic queries
without spawning a PowerShell process for every lookup.

Writes are intentionally NOT exposed.
"""
import sys
from typing import Optional

HIVE_NAMES = {
    "HKLM": "HKEY_LOCAL_MACHINE",
    "HKCU": "HKEY_CURRENT_USER",
    "HKCR": "HKEY_CLASSES_ROOT",
    "HKU":  "HKEY_USERS",
    "HKCC": "HKEY_CURRENT_CONFIG",
}


def _open_key(full_path: str):
    if sys.platform != "win32":
        raise RuntimeError("registry_* tools are Windows-only")
    import winreg

    parts = full_path.replace("/", "\\").split("\\", 1)
    if not parts:
        raise ValueError("empty registry path")
    hive_alias = parts[0].upper()
    hive_name = HIVE_NAMES.get(hive_alias, hive_alias)
    hive = getattr(winreg, hive_name, None)
    if hive is None:
        raise ValueError(f"unknown hive: {hive_alias} (use HKLM, HKCU, HKCR, HKU, HKCC)")
    subkey = parts[1] if len(parts) > 1 else ""
    return winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)


def registry_read(key_path: str, value_name: Optional[str] = None) -> str:
    """Read a value (or all values + subkeys) from the Windows registry.

    Args:
        key_path: Registry key, e.g. "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion".
        value_name: Specific value name to read. Omit to list all values and subkeys.
    """
    try:
        import winreg

        with _open_key(key_path) as k:
            if value_name is not None:
                data, typ = winreg.QueryValueEx(k, value_name)
                return f"{value_name} = {data!r}  (type={typ})"
            # list all
            num_values, num_subkeys = (0, 0)
            try:
                num_subkeys, num_values, _ = winreg.QueryInfoKey(k)
            except OSError:
                pass

            lines = [f"key: {key_path}", f"values: {num_values}", f"subkeys: {num_subkeys}", ""]
            lines.append("[values]")
            for i in range(num_values):
                try:
                    name, data, typ = winreg.EnumValue(k, i)
                    name_disp = name or "(Default)"
                    disp = repr(data)
                    if len(disp) > 120:
                        disp = disp[:120] + "..."
                    lines.append(f"  {name_disp}  =  {disp}")
                except OSError:
                    continue
            lines.append("")
            lines.append("[subkeys]")
            for i in range(num_subkeys):
                try:
                    lines.append("  " + winreg.EnumKey(k, i))
                except OSError:
                    continue
            return "\n".join(lines)
    except FileNotFoundError:
        return f"[registry_read: key not found: {key_path}]"
    except PermissionError:
        return f"[registry_read error: permission denied for {key_path}]"
    except Exception as e:
        return f"[registry_read error: {type(e).__name__}: {e}]"


def service_status(name: Optional[str] = None) -> str:
    """List Windows services or show one service's status.

    Args:
        name: Specific service name (e.g. "Spooler"). Omit to list all services.
    """
    try:
        import psutil
    except ImportError:
        return "[service_status error: psutil not installed]"
    try:
        if name:
            try:
                svc = psutil.win_service_get(name)
                info = svc.as_dict()
            except Exception:
                return f"[service_status: service not found: {name}]"
            lines = [f"name: {info.get('name')}"]
            lines.append(f"display: {info.get('display_name')}")
            lines.append(f"status: {info.get('status')}")
            lines.append(f"start_type: {info.get('start_type')}")
            lines.append(f"username: {info.get('username')}")
            lines.append(f"pid: {info.get('pid')}")
            lines.append(f"binpath: {info.get('binpath')}")
            return "\n".join(lines)

        rows = []
        for svc in psutil.win_service_iter():
            try:
                d = svc.as_dict()
                rows.append((d.get("name") or "", d.get("status") or "", d.get("start_type") or ""))
            except Exception:
                continue
        rows.sort(key=lambda r: r[0].lower())
        lines = [f"{'NAME':<40}  {'STATUS':<10}  START_TYPE"]
        for n, s, st in rows:
            lines.append(f"{n[:40]:<40}  {s:<10}  {st}")
        lines.append(f"--- {len(rows)} service(s)")
        return "\n".join(lines)
    except AttributeError:
        return "[service_status error: not on Windows]"
    except Exception as e:
        return f"[service_status error: {type(e).__name__}: {e}]"
