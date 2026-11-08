"""The Windows memory/CPU probes must be prototyped once at import, not re-resolved per call.

Why this test exists. On 2026-11-07 python.exe crashed repeatedly on the operator's PC with
0xc0000005 access violations (and one 0xc0000374 heap corruption) at varying offsets inside
python310.dll -- the signature of one thread corrupting the heap and another thread later
stepping on it. The fault rode on avail_phys_mb() in relay/relay_fleet.py and the probes in
relay/stability_replay.py, which reached ``ctypes.windll.kernel32.GlobalMemoryStatusEx`` (and
GetSystemTimes) with no argtypes. Two defects compounded there:

  1. ``ctypes.windll`` is a lazy attribute cache; several fleet worker threads poll
     avail_phys_mb() at once (the admission loop, autoscale, should_recycle), and racing the
     FIRST attribute access builds the FFI prototype concurrently, which is not thread-safe.
  2. With no argtypes, ctypes passes the ``byref(m)`` pointer as a C int and can truncate it
     on Win64, so the API writes nine 8-byte fields through a mangled pointer -- a heap write
     past the structure.

The fix binds a single WinDLL handle with explicit argtypes/restype at import time. These
tests pin that shape. They assert on SOURCE and on the module-level pre-bound objects rather
than by driving the native call, so they run on the Linux CI runner where the probe degrades
to a fallback and never touches the WinAPI.
"""
import inspect

import relay.relay_fleet as RF
import relay.stability_replay as SR


def test_avail_phys_mb_does_not_lazily_resolve_windll_per_call():
    """The per-call ``ctypes.windll.kernel32.GlobalMemoryStatusEx`` is what raced across
    worker threads. If it comes back, the corruption window comes back with it."""
    src = inspect.getsource(RF.avail_phys_mb)
    assert "ctypes.windll" not in src, (
        "avail_phys_mb re-resolves the WinAPI per call via ctypes.windll; it must call the "
        "pre-bound handle instead")


def test_relay_fleet_prebinds_globalmemorystatusex_with_argtypes():
    """A module-level handle with explicit argtypes is the whole fix: the prototype exists
    before any worker runs, and the pointer is passed full-width."""
    assert hasattr(RF, "_GlobalMemoryStatusEx"), "the pre-bound handle is missing"
    src = inspect.getsource(RF)
    assert ".argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]" in src, (
        "GlobalMemoryStatusEx must declare its pointer argtype so Win64 does not truncate it")
    assert "_GlobalMemoryStatusEx.restype" in src


def test_avail_phys_mb_is_safe_and_finite_on_every_platform():
    """The function must return a positive float without raising on ANY platform: off Windows
    the handle is None and it falls back; on Windows it calls the pre-bound handle. A probe
    that throws would take down every caller that gates on free RAM."""
    v = RF.avail_phys_mb()
    assert isinstance(v, float)
    assert v > 0.0


def test_ram_room_for_tab_still_answers_off_windows():
    """ram_room_for_tab() rides on avail_phys_mb(); it must keep returning a bool, not raise."""
    assert isinstance(RF.ram_room_for_tab(), bool)


def test_stability_replay_does_not_lazily_resolve_windll():
    """The same lazy-windll pattern lived in the replay harness's two probes."""
    src = inspect.getsource(SR)
    assert "ctypes.windll" not in src, (
        "stability_replay still reaches the WinAPI via ctypes.windll per call")


def test_stability_replay_prebinds_both_probes_with_argtypes():
    """Both GlobalMemoryStatusEx and GetSystemTimes must be pre-bound with pointer argtypes."""
    assert hasattr(SR, "_GlobalMemoryStatusEx")
    assert hasattr(SR, "_GetSystemTimes")
    src = inspect.getsource(SR)
    assert "_GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMSTAT)]" in src
    assert "_GetSystemTimes.argtypes" in src


def test_stability_replay_probes_are_safe_on_every_platform():
    """Both probes must degrade quietly rather than raise: off Windows the handles are None and
    read_cpu_percent returns None; on Windows they return a measured value. Either way the
    call must not throw and sample_resources must return a dict."""
    import sys
    cpu = SR.read_cpu_percent()
    if sys.platform.startswith("win"):
        assert cpu is None or isinstance(cpu, float)
    else:
        assert cpu is None
    out = SR.sample_resources()
    assert isinstance(out, dict)
