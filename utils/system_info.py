"""Windows system helpers: DPI awareness, display geometry, memory stats.

Everything here stays on the standard library + ctypes to keep the runtime
footprint small and avoid anti-virus false positives.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QRect
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

_DWMWA_USE_IMPINFORCEDDARK = 20  # modern dark caption (unused here, kept for reference)

# Per-Monitor DPI Aware context values (user32)
_DpiAwarenessContextPerMonitorAwareV2 = ctypes.c_void_p(-4)
_PROCESS_PER_MONITOR_DPI_AWARE = 2

_dpi_configured = False


def set_dpi_awareness() -> None:
    """Mark the process as Per-Monitor DPI Aware v2.

    Must be called from the main thread before a QApplication (or any QWidget)
    is created so Windows does not bitmap-scale our frameless window.
    """
    global _dpi_configured
    if _dpi_configured:
        return
    try:
        user32 = ctypes.windll.user32
        ctx = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if ctx:
            ctx.argtypes = [ctypes.c_void_p]
            ctx.restype = wintypes.BOOL
            if not ctx(_DpiAwarenessContextPerMonitorAwareV2):
                # Older Windows: fall back to the classic per-monitor call.
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)
                except AttributeError:
                    user32.SetProcessDPIAware()
        else:
            user32.SetProcessDPIAware()
    except Exception:
        pass
    _dpi_configured = True


def screen_for_cursor() -> object:
    """Return the QScreen under the current cursor, preferring it over primary."""
    if QApplication.instance() is None:
        return None
    try:
        screen = QApplication.screenAt(QCursor.pos())
    except Exception:
        screen = None
    return screen or QApplication.primaryScreen()


def centered_rect(available: QRect, width: int, height: int) -> QRect:
    """Center a `width` x `height` rectangle inside `available` (pure math)."""
    x = available.x() + (available.width() - width) // 2
    y = available.y() + (available.height() - height) // 2
    return QRect(x, y, width, height)


def current_memory_usage_mb() -> float:
    """Working set of the current process in MiB via GetProcessMemoryInfo."""
    try:
        import ctypes.wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        psapi = ctypes.windll.psapi
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.sizeof(counters)
        ):
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return 0.0